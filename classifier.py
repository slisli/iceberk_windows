'''
mpiclassify
====
Provides an MPI interface that trains linear classifiers that can be represented
by
    \min_w     1/N * sum_n L(y_n,w'x_n+b) + gamma * Reg(w)

This algorithm only deals with the primal case (no dual), assuming that there 
are more data points than the number of feature dimension (if not, you might 
want to look for dual solvers to your problem). We use L-BFGS as the default
solver, and if the loss function or regularizer is not differentiable everywhere
(like the v-style L1 regularizer), we will use the subgradient methods.
'''

from iceberk import cpputil, mpi, mathutil
import logging
import numpy as np
from scipy import optimize
from sklearn import metrics


_FMIN = optimize.fmin_l_bfgs_b

def to_one_of_k_coding(Y, fill = -1):
    '''Convert the vector Y into one-of-K coding. The element will be either
    fill (-1 in default) or 1
    '''
    if Y.ndim > 1:
        raise ValueError, "The input Y should be a vector."
    K = mpi.COMM.allreduce(Y.max(), op=max) + 1
    Yout = np.ones((len(Y), K)) * fill
    Yout[np.arange(len(Y)), Y.astype(int)] = 1
    return Yout

def feature_meanstd(mat, reg = None):
    '''
    Utility function that does distributed mean and std computation
    Input:
        mat: the local data matrix, each row is a feature vector and each 
             column is a feature dim
        reg: if reg is not None, the returned std is computed as
            std = np.sqrt(std**2 + reg)
    Output:
        m:      the mean for each dimension
        std:    the standard deviation for each dimension
    
    The implementation is actually moved to iceberk.mathutil now, we leave the
    code here just for backward compatibility
    '''
    m, std = mathutil.mpi_meanstd(mat)

    if reg is not None:
        std = np.sqrt(std**2 + reg)
    return m, std


class Solver(object):
    '''
    Solver is the general solver to deal with bookkeeping stuff
    '''
    def __init__(self, gamma, loss, reg,
                 lossargs = {}, regargs = {}, fminargs = {}):
        '''
        Initializes the solver.
        Input:
            gamma: the regularization parameter
            loss: the loss function. Should accept three variables Y, X and W,
                where Y is a vector in {labels}^(num_data), X is a matrix of size
                [num_data,nDim], and W is a vector of size nDim. It returns
                the loss function value and the gradient with respect to W.
            reg: the regularizaiton func. Should accept a vector W of
                shape nDim and returns the regularization term value and
                the gradient with respect to W.
            lossargs: the arguments that should be passed to the loss function
            regargs: the arguments that should be passed to the regularizer
            fminargs: additional arguments that you may want to pass to fmin.
                you can check the fmin function to see what arguments can be
                passed (like display options: {'disp':1}).
        '''
        self._gamma = gamma
        self.loss = loss
        self.reg = reg
        self._lossargs = lossargs
        self._regargs = regargs
        self._fminargs = fminargs
        self._add_default_fminargs()
    
    def _add_default_fminargs(self):
        '''
        This function adds some default args to fmin, if we have not explicitly
        specified them.
        '''
        self._fminargs['maxfun'] = self._fminargs.get('maxfun', 1000)
        self._fminargs['disp'] = self._fminargs.get('disp', 1)
        # even when fmin displays outputs, we set non-root display to none
        if not mpi.is_root():
            self._fminargs['disp'] = 0
            
    @staticmethod
    def obj(wb, solver):
        """The objective function to be used by fmin
        """
        raise NotImplementedError
    
    def presolve(self, X, Y, weight, param_init):
        """This function is called before we call lbfgs. It should return a
        vector that is the initialization of the lbfgs, and does any preparation
        (such as creating caches) for the optimization.
        """
        raise NotImplementedError
    
    def postsolve(self, lbfgs_result):
        """This function deals with the post-processing of the lbfgs result. It
        should return the optimal parameter for the classifier.
        """
        raise NotImplementedError
    
    def solve(self, X, Y, weight = None, param_init = None):
        """The solve function
        """
        param_init = self.presolve(X, Y, weight, param_init)
        logging.debug('Solver: running lbfgs...')
        result = _FMIN(self.__class__.obj, param_init, 
                       args=[self], **self._fminargs)
        return self.postsolve(result)


class SolverSC(Solver):
    """The solver that does single-class classification
    Output:
        w, b :      the learned weights and bias
    """
    
    def presolve(self, X, Y, weight, param_init):
        self._X = X.reshape((X.shape[0],np.prod(X.shape[1:])))
        self._Y = Y
        self._weight = weight
        # compute the number of data
        if weight is None:
            self._num_data = mpi.COMM.allreduce(X.shape[0])
        else:
            self._num_data = mpi.COMM.allreduce(weight.sum())
        self._dim = self._X.shape[1]
        # prediction cache
        self._pred = np.empty(X.shape[0], dtype=X.dtype)
        if param_init is None:
            param_init = np.zeros(self._dim+1)
        elif len(param_init) == 2:
            # the initialization is w and b
            param_init = np.hstack((param_init[0].flatten(), 
                                    param_init[1].flatten()))
        # gradient cache
        self._glocal = np.empty(param_init.shape)
        self._g = np.empty(param_init.shape)
        # just to make sure every node is on the same page
        mpi.COMM.Bcast(param_init)
        return param_init
    
    def postsolve(self, lbfgs_result):
        return lbfgs_result[0][:-1], lbfgs_result[0][-1]
    
    @staticmethod
    def obj(param, solver):
        '''The objective function used by fmin
        '''
        w = param[:-1]
        b = param[-1]
        # prediction is a vector
        np.dot(solver._X, w, out=solver._pred)
        solver._pred += b
        # call the loss
        flocal, gpred = solver.loss(solver._Y, solver._pred, solver._weight,
                                   **solver._lossargs)
        # get the gradient for both w and b
        np.dot(gpred, solver._X, out=solver._glocal[:-1])
        solver._glocal[-1] = gpred.sum()
        # do mpi reduction
        # for the regularization term
        freg, greg = solver.reg(w, **solver._regargs)
        flocal += solver._num_data * solver._gamma / mpi.SIZE * freg
        solver._glocal[:-1] += solver._num_data * solver._gamma / mpi.SIZE * greg
        
        mpi.barrier()
        f = mpi.COMM.allreduce(flocal)
        mpi.COMM.Allreduce(solver._glocal, solver._g)
        return f, solver._g


class SolverMC(Solver):
    '''SolverMC is a multi-dimensional wrapper
    For the input Y, it could be either a vector of the labels
    (starting from 0), or a matrix whose values are -1 or 1. You 
    need to manually make sure that the input Y format is consistent
    with the loss function though.
    '''
    
    def presolve(self, X, Y, weight, param_init):
        self._X = X.reshape((X.shape[0],np.prod(X.shape[1:])))
        if len(Y.shape) == 1:
            self._K = mpi.COMM.allreduce(Y.max(), op=max) + 1
        else:
            # We treat Y as a two-dimensional matrix
            Y = Y.reshape((Y.shape[0],np.prod(Y.shape[1:])))
            self._K = Y.shape[1]
        self._Y = Y
        self._weight = weight
        # compute the number of data
        if weight is None:
            self._num_data = mpi.COMM.allreduce(X.shape[0])
        else:
            self._num_data = mpi.COMM.allreduce(weight.sum())
        self._dim = self._X.shape[1]
        self._pred = np.empty((X.shape[0], self._K), dtype = X.dtype)
        if param_init is None:
            param_init = np.zeros(self._K * (self._dim+1))
        elif len(param_init) == 2:
            # the initialization is w and b
            param_init = np.hstack((param_init[0].flatten(), 
                                    param_init[1].flatten()))
        # gradient cache
        self._glocal = np.empty(param_init.shape)
        self._g = np.empty(param_init.shape)
        # just to make sure every node is on the same page
        mpi.COMM.Bcast(param_init)
        return param_init
    
    def postsolve(self, lbfgs_result):
        wb = lbfgs_result[0]
        K = self._K
        w = wb[: K * self._dim].reshape(self._dim, K).copy()
        b = wb[K * self._dim :].copy()
        return w, b
    
    @staticmethod
    def obj(wb,solver):
        '''
        The objective function used by fmin
        '''
        # obtain w and b
        K = solver._K
        dim = solver._dim
        w = wb[:K*dim].reshape((dim, K))
        b = wb[K*dim:]
        # pred is a matrix of size [num_datalocal, K]
        mathutil.dot(solver._X, w, out = solver._pred)
        solver._pred += b
        # compute the loss function
        flocal,gpred = solver.loss(solver._Y, solver._pred, solver._weight,
                                   **solver._lossargs)
        mathutil.dot(solver._X.T, gpred, out = solver._glocal[:K*dim].reshape(dim, K))
        solver._glocal[K*dim:] = gpred.sum(axis=0)
        
        # add regularization term, but keep in mind that we have multiple nodes
        freg, greg = solver.reg(w, **solver._regargs)
        flocal += solver._num_data * solver._gamma * freg / mpi.SIZE
        solver._glocal[:K*dim] += solver._num_data * solver._gamma / mpi.SIZE \
                          * greg.ravel()
        # do mpi reduction
        mpi.barrier()
        f = mpi.COMM.allreduce(flocal)
        mpi.COMM.Allreduce(solver._glocal, solver._g)
        return f, solver._g


class Loss(object):
    """LOSS defines commonly used loss functions
    For all loss functions:
    Input:
        Y:    a vector or matrix of true labels
        pred: prediction, has the same shape as Y.
    Return:
        f: the loss function value
        g: the gradient w.r.t. pred, has the same shape as pred.
    """
    def __init__(self):
        """All functions in Loss should be static
        """
        raise NotImplementedError, "Loss should not be instantiated!"
    
    @staticmethod
    def loss_l2(Y, pred, weight, **kwargs):
        '''
        The l2 loss: f = ||Y - pred||_{fro}^2
        '''
        diff = pred - Y
        if weight is None:
            return np.dot(diff.flat, diff.flat), 2.*diff 
        else:
            return np.dot((diff**2).sum(1), weight), \
                   2.*diff*weight[:,np.newaxis]
        
    @staticmethod
    def loss_hinge(Y, pred, weight, **kwargs):
        '''The SVM hinge loss. Input vector Y should have values 1 or -1
        '''
        margin = np.maximum(0., 1. - Y * pred)
        if weight is None:
            f = margin.sum()
            g = - Y * (margin>0)
        else:
            f = np.dot(weight, margin).sum()
            g = - Y * weight * (margin>0)
        return f, g
    
    @staticmethod
    def loss_squared_hinge(Y,pred,weight,**kwargs):
        ''' The squared hinge loss. Input vector Y should have values 1 or -1
        '''
        margin = np.maximum(0., 1. - Y * pred)
        if weight is None:
            return np.dot(margin.flat, margin.flat), -2.*Y*margin
        else:
            return np.dot(weight, margin**2).sum(), -2.*Y*weight*margin
        
    @staticmethod
    def loss_bnll(Y,pred,weight,**kwargs):
        '''
        the BNLL loss: f = log(1 + exp(-y * pred))
        '''
        # expnyp is exp(-y * pred)
        expnyp = mathutil.exp(-Y*pred)
        expnyp_plus = 1. + expnyp
        if weight is None:
            return np.sum(np.log(expnyp_plus)), -Y * expnyp / expnyp_plus
        else:
            return np.dot(weight, np.log(expnyp_plus)).sum(), \
                   - Y * weight * expnyp / expnyp_plus

    @staticmethod
    def loss_multiclass_logistic(Y, pred, weight, **kwargs):
        """The multiple class logistic regression loss function
        
        The input Y should be a 0-1 matrix 
        """
        # normalized prediction and avoid overflowing
        prob = pred - pred.max(axis=1)[:,np.newaxis]
        mathutil.exp(prob, out=prob)
        prob /= prob.sum(axis=1)[:, np.newaxis]
        g = prob - Y
        # take the log
        mathutil.log(prob, out=prob)
        return -np.dot(prob.flat, Y.flat), g


    @staticmethod
    def loss_rank_hinge(Y, pred, weight, **kwargs):
        """The rank loss: the score of the true label should be higher
        than the other scores by a margin, and hinge loss is used to compute
        the loss.
        
        Input:
            Y: a vector indicating the true labels
            pred: a matrix indicating the scores for each label
        """
        N = len(Y)
        score_gt = pred[np.arange(N), Y]
        diff = pred - (score_gt-1.)[:, np.newaxis]
        # diff_hinge will be the hinge loss for each class, except for the 
        # ground truth where it should be 0 (instead of 1)
        diff_hinge = np.maximum(diff, 0.)
        if weight is None:
            # for the loss we will subtract N due to the ground truth offset
            f = diff_hinge.sum() - N
            # for the gradient of non-ground truth predictions, it's simply
            # a boolean value. For the ground truth prediction, it's the sum of
            # the violations
            g = (diff > 0).astype(np.float64)
            g[np.arange(N), Y] = 1. - g.sum(axis=1)
        else:
            raise NotImplementedError
        return f, g
    
    @staticmethod
    def loss_rank_squared_hinge(Y, pred, weight, **kwargs):
        """The rank-based squared hinge loss
        """
        raise NotImplementedError, "Yangqing still needs to debug this"
        N = len(Y)
        score_gt = pred[np.arange(N), Y]
        diff = pred - (score_gt-1.)[:, np.newaxis]
        # diff_hinge will be the hinge loss for each class, except for the 
        # ground truth where it should be 0 (instead of 1)
        diff_hinge = np.maximum(diff, 0.)
        if weight is None:
            # for the loss we will subtract N due to the ground truth offset
            f = np.dot(diff_hinge.flat, diff_hinge.flat) - N
            # for the gradient of non-ground truth predictions, it's simply
            # a boolean value. For the ground truth prediction, it's the sum of
            # the violations
            g = 2. * diff_hinge
            g[np.arange(N), Y] = 2. - g.sum(axis=1)
        else:
            f = np.dot(weight, diff_hinge).sum()
            g = - 2. * diff_hinge
            g[np.arange(N), Y] = 2. - g.sum(axis=1)
            g *= weight[:, np.newaxis]
        return f, g


class Reg(object):
    '''
    REG defines commonly used regularization functions
    For all regularization functions:
    Input:
        w: the weight vector, or the weight matrix in the case of multiple classes
    Return:
        f: the regularization function value
        g: the gradient w.r.t. w, has the same shape as w.
    '''
    @staticmethod
    def reg_l2(w,**kwargs):
        '''
        l2 regularization: ||w||_2^2
        '''
        return np.dot(w.flat, w.flat), 2.*w

    @staticmethod
    def reg_l1(w,**kwargs):
        '''
        l1 regularization: ||w||_1
        '''
        g = np.sign(w)
        # subgradient
        g[g==0] = 0.5
        return np.abs(w).sum(), g

    @staticmethod
    def reg_elastic(w, **kwargs):
        '''
        elastic net regularization: (1-alpha) * ||w||_2^2 + alpha * ||w||_1
        kwargs['alpha'] is the balancing weight, default 0.5
        '''
        alpha1 = kwargs.get('alpha', 0.5)
        alpha2 = 1. - alpha1
        f1, g1 = Reg.reg_l1(w, **kwargs)
        f2, g2 = Reg.reg_l2(w, **kwargs)
        return f1 * alpha1 + f2 * alpha2, g1 * alpha1 + g2 * alpha2

class Evaluator(object):
    """Evaluator implements some commonly-used criteria for evaluation
    """
    @staticmethod
    def mse(Y, pred, axis=None):
        """Return the mean squared error of the true value and the prediction
        Input:
            Y, pred: the true value and the prediction
            axis: (optional) if Y and pred are matrices, you can specify the
                axis along which the mean is carried out.
        """
        return ((Y - pred) ** 2).mean(axis=axis)
    
    @staticmethod
    def accuracy(Y, pred):
        """Computes the accuracy
        Input: 
            Y, pred: two vectors containing discrete labels. If either is a
            matrix instead of a vector, then argmax is used to get the discrete
            labels.
        """
        if pred.ndim == 2:
            pred = pred.argmax(axis=1)
        if Y.ndim == 2:
            Y = Y.argmax(axis=1)
        correct = mpi.COMM.allreduce((Y==pred).sum())
        num_data = mpi.COMM.allreduce(len(Y))
        return float(correct) / num_data
    
    @staticmethod
    def confusion_table(Y, pred):
        """Computes the confusion table
        Input:
            Y, pred: two vectors containing discrete labels
        Output:
            table: the confusion table. table[i,j] is the number of data points
                that belong to i but predicted as j
        """
        if pred.ndim == 2:
            pred = pred.argmax(axis=1)
        if Y.ndim == 2:
            Y = Y.argmax(axis=1)
        num_classes = Y.max() + 1
        table = np.zeros((num_classes, num_classes))
        for y, p in zip(Y, pred):
            table[y,p] += 1
        return table
    
    @staticmethod
    def accuracy_class_averaged(Y, pred):
        """Computes the accuracy, but averaged over classes instead of averaged
        over data points.
        Input:
            Y: the ground truth vector
            pred: a vector containing the predicted labels. If pred is a matrix
            instead of a vector, then argmax is used to get the discrete label.
        """
        if pred.ndim == 2:
            pred = pred.argmax(axis=1)
        num_classes = Y.max() + 1
        accuracy = 0.0
        correct = (Y == pred).astype(np.float)
        for i in range(num_classes):
            idx = (Y == i)
            accuracy += correct[idx].mean()
        accuracy /= num_classes
        return accuracy

    @staticmethod
    def top_k_accuracy(Y, pred, k):
        """Computes the top k accuracy
        Input:
            Y: a vector containing the discrete labels of each datum
            pred: a matrix of size len(Y) * num_classes, each row containing the
                real value scores for the corresponding label. The classes with
                the highest k scores will be considered.
        """
        if k > pred.shape[1]:
            logging.warning("Warning: k is larger than the number of classes"
                            "so the accuracy would always be one.")
        top_k_id = np.argsort(pred, axis=1)[:, -k:]
        match = (top_k_id == Y[:, np.newaxis])
        correct = mpi.COMM.allreduce(match.sum())
        num_data = mpi.COMM.allreduce(len(Y))
        return float(correct) / num_data
    
    @staticmethod
    def average_precision(Y, pred):
        """Average Precision for binary classification
        """
        # since we need to compute the precision recall curve, we have to
        # compute this on the root node.
        Y = mpi.COMM.gather(Y)
        pred = mpi.COMM.gather(pred)
        if mpi.is_root():
            Y = np.hstack(Y)
            pred = np.hstack(pred)
            precision, recall, _ = metrics.precision_recall_curve(
                    Y == 1, pred)
            ap = metrics.auc(recall, precision)
        else:
            ap = None
        mpi.barrier()
        return mpi.COMM.bcast(ap)
    
    @staticmethod
    def average_precision_multiclass(Y, pred):
        """Average Precision for multiple class classification
        """
        K = pred.shape[1]
        aps = [Evaluator.average_precision(Y==k, pred[:,k]) for k in range(K)]
        return np.asarray(aps).mean()

'''
Utility functions that wraps often-used functions
'''
    
def svm_binary(X, Y, gamma, weight = None, **kwargs):
    solver = SolverSC(gamma, Loss.loss_hinge, Reg.reg_l2, **kwargs)
    return solver.solve(X, Y, weight)

def l2svm_binary(X, Y, gamma, weight = None, **kwargs):
    solver = SolverSC(gamma, Loss.loss_squared_hinge, Reg.reg_l2, **kwargs)
    return solver.solve(X, Y, weight)

def svm_onevsall(X, Y, gamma, weight = None, **kwargs):
    if Y.ndim == 1:
        Y = to_one_of_k_coding(Y)
    solver = SolverMC(gamma, Loss.loss_hinge, Reg.reg_l2, **kwargs)
    return solver.solve(X, Y, weight)

def l2svm_onevsall(X, Y, gamma, weight = None, **kwargs):
    if Y.ndim == 1:
        Y = to_one_of_k_coding(Y)
    solver = SolverMC(gamma, Loss.loss_squared_hinge, Reg.reg_l2, **kwargs)
    return solver.solve(X, Y, weight)

def svm_multiclass(X, Y, gamma, weight = None, **kwargs):
    solver = SolverMC(gamma, Loss.loss_rank_hinge, Reg.reg_l2, **kwargs)
    return solver.solve(X, Y, weight)

def l2svm_multiclass(X, Y, gamma, weight = None, **kwargs):
    solver = SolverMC(gamma, Loss.loss_rank_squared_hinge, Reg.reg_l2, **kwargs)
    return solver.solve(X, Y, weight)

def elasticnet_svm_multiclass(X, Y, gamma, weight = None, alpha = 0.5, **kwargs):
    solver = SolverMC(gamma, Loss.loss_rank_squared_hinge, Reg.reg_elastic, 
                      lossargs = {'alpha': alpha}, **kwargs)
    return solver.solve(X, Y, weight)
