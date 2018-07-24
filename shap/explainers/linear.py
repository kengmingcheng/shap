import numpy as np
import warnings
from .explainer import Explainer

class LinearExplainer(Explainer):
    """ Computes SHAP values for a linear model, optionally accounting for inter-feature correlations.

    This computes the SHAP values for a linear model and can account for the correlations among
    the input features. Assuming features are independent leads to interventional SHAP values which
    for a linear model are coef[i] * (x[i] - X.mean(0)[i]) for the ith feature. If instead we account
    for correlations then we prevent any problems arising from colinearity and share credit among
    correlated features. Accounting for correlations can be computationally challenging, but
    LinearExplainer uses sampling to estimate a transform that can then be applied to explain
    any prediction of the model.

    Parameters
    ----------
    model : (coef, intercept) or sklearn.linear_model.*
        User supplied linear model either as either a parameter pair or sklearn object.

    data : (mean, cov), numpy.array, pandas.DataFrame, or iml.DenseData
        The background dataset to use for computing conditional expectations. Note that only the
        mean and covariance of the dataset are used. This means passing a raw data matrix is just
        a convienent alternative to passing the mean and covariance directly.
    nsamples : int
        Number of samples to use when estimating the transformation matrix used to account for
        feature correlations.
    feature_dependence : "correlation" (default) or "interventional"
        There are two ways we might want to compute SHAP values, either the full conditional SHAP
        values or the interventional SHAP values. For interventional SHAP values we break any
        dependence structure in the model and so uncover how the model would behave if we
        intervened and changed some of the inputs. For the full conditional SHAP values we respect
        the correlations among the input features, so if the model depends on one input but that
        input is correlated with another input, then both get some credit for the model's behavior.
    """

    def __init__(self, model, data, nsamples=1000, feature_dependence="correlation"):
        self.nsamples = nsamples
        self.feature_dependence = feature_dependence

        # raw coefficents
        if type(model) == tuple and len(model) == 2:
            self.coef = model[0]
            self.intercept = model[1]

        # sklearn style model
        elif hasattr(model, "coef_") and hasattr(model, "intercept_"):
            self.coef = model.coef_
            self.intercept = model.intercept_

        else:
            raise Exception("An unknown model type was passed: " + str(type(model)))

        # convert DataFrame's to numpy arrays
        if str(type(data)).endswith("'pandas.core.frame.DataFrame'>"):
            data = data.values

        # get the mean and covariance of the model
        if type(data) == tuple and len(data) == 2:
            self.mean = data[0]
            self.cov = data[1]
        elif data is None and feature_dependence == "correlation":
            raise Exception("A background data distriubtion must be provided when feature_dependence='correlation'!")
        elif str(type(data)).endswith("'numpy.ndarray'>"):
            self.mean = data.mean(0)
            self.cov = np.cov(data, rowvar=False)

        # if needed, estimate the transform matrices
        if feature_dependence == "correlation":
            mean_transform, x_transform = self._estimate_transforms(nsamples)
            self.mean_transformed = mean_transform @ self.mean
            self.x_transform = x_transform
        elif feature_dependence == "interventional":
            if nsamples != 1000:
                warnings.warn("Setting nsamples has no effect when feature_dependence = 'interventional'!")
        else:
            raise Exception("Unknown type of feature_dependence provided: " + feature_dependence)

    def _estimate_transforms(self, nsamples):
        """ Uses block matrix inversion identities to quickly estimate transforms.

        After a bit of matrix math we can isolate a transoform matrix (# features x # features)
        that is independent of any sample we are explaining. It is the result of averaging over
        all feature permutations, but we just use a fixed number of samples to estimate the value.

        TODO: Do a brute force enumeration when # feature subsets is less than nsamples. This could
              happen through a recursive method that uses the same block matrix inversion as below.
        """
        coef = self.coef
        cov = self.cov
        M = len(self.coef)

        mean_transform = np.zeros((M,M))
        x_transform = np.zeros((M,M))
        inds = np.arange(M, dtype=np.int)
        for _ in range(nsamples):
            np.random.shuffle(inds)
            cov_inv_SiSi = np.zeros((0,0))
            cov_Si = np.zeros((M,0))
            for j in range(M):
                i = inds[j]

                # use the last Si as the new S
                cov_S = cov_Si
                cov_inv_SS = cov_inv_SiSi

                # get the new cov_Si
                cov_Si = self.cov[:,inds[:j+1]]

                # compute the new cov_inv_SiSi from cov_inv_SS
                d = cov_Si[i,:-1].T
                t = cov_inv_SS @ d
                Z = cov[i, i]
                u = Z - t.T @ d
                cov_inv_SiSi = np.zeros((j+1, j+1))
                if j > 0:
                    cov_inv_SiSi[:-1, :-1] = cov_inv_SS + np.outer(t, t) / u
                    cov_inv_SiSi[:-1, -1] = cov_inv_SiSi[-1,:-1] = -t / u
                cov_inv_SiSi[-1, -1] = 1 / u

                # + coef @ (Q(bar(Sui)) - Q(bar(S)))
                mean_transform[i, i] += self.coef[i]

                # + coef @ R(Sui)
                coef_R_Si = self.coef[inds[j+1:]] @ (cov_Si @ cov_inv_SiSi)[inds[j+1:]]
                mean_transform[i, inds[:j+1]] += coef_R_Si

                # - coef @ R(S)
                coef_R_S = self.coef[inds[j:]] @ (cov_S @ cov_inv_SS)[inds[j:]]
                mean_transform[i, inds[:j]] -= coef_R_S

                # - coef @ (Q(Sui) - Q(S))
                x_transform[i, i] += self.coef[i]

                # + coef @ R(Sui)
                x_transform[i, inds[:j+1]] += coef_R_Si

                # - coef @ R(S)
                x_transform[i, inds[:j]] -= coef_R_S

        mean_transform /= nsamples
        x_transform /= nsamples
        return mean_transform, x_transform

    def shap_values(self, X):
        """ Estimate the SHAP values for a set of samples.

        Parameters
        ----------
        X : numpy.array or pandas.DataFrame
            A matrix of samples (# samples x # features) on which to explain the model's output.

        Returns
        -------
        For models with a single output this returns a matrix of SHAP values
        (# samples x # features). Each row sums to the difference between the model output for that
        sample and the expected value of the model output (which is stored as expected_value
        attribute of the explainer).
        """

        # convert dataframes
        if str(type(X)).endswith("pandas.core.series.Series'>"):
            X = X.values
        elif str(type(X)).endswith("'pandas.core.frame.DataFrame'>"):
            if self.keep_index:
                index_value = X.index.values
                index_name = X.index.name
                column_name = list(X.columns)
            X = X.values

        assert str(type(X)).endswith("'numpy.ndarray'>"), "Unknown instance type: " + str(type(X))
        assert len(X.shape) == 1 or len(X.shape) == 2, "Instance must have 1 or 2 dimensions!"

        if self.feature_dependence == "correlation":
            return X @ self.x_transform.T - self.mean_transformed
        elif self.feature_dependence == "interventional":
            return self.coef * (X - self.mean)
