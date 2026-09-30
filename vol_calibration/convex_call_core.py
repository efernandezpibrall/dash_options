"""Quote-preserving convex call-price fallback for a failing TTF variance core.

The anchor call prices stay fixed. Only their cubic-Hermite derivatives are
fitted, under linear constraints on both endpoint gammas of every interval.
Since cubic gamma is linear, those constraints govern the entire interval.
Black inversion maps the resulting prices back to total variance without
interpolating (or adjusting) the original volatility anchors.
"""

from collections import OrderedDict

import numpy as np
from scipy.interpolate import CubicHermiteSpline
from scipy.optimize import LinearConstraint, minimize
from scipy.special import ndtr


def _normalized_calls(k, variance):
    root = np.sqrt(variance)
    d1 = -np.log(k) / root + 0.5 * root
    return ndtr(d1) - k * ndtr(d1 - root)


def _normal_pdf(values):
    return np.exp(-0.5 * values**2) / np.sqrt(2.0 * np.pi)


class ConvexCallTotalVariance:
    """Total-variance callable with the boundary derivative interface of PCHIP."""

    def __init__(self, x_nodes, variance_nodes, calls, derivatives):
        self.x_nodes = np.asarray(x_nodes, dtype=float).copy()
        self.variance_nodes = np.asarray(variance_nodes, dtype=float).copy()
        self.calls = CubicHermiteSpline(
            np.exp(self.x_nodes), calls, derivatives, extrapolate=False
        )
        # Solver gate grids repeat while only Wing parameters change. Keep a
        # bounded, instance-local cache of immutable variance evaluations.
        self._variance_cache = OrderedDict()

    def __call__(self, x):
        values = np.asarray(x, dtype=float)
        flat = np.ascontiguousarray(values.reshape(-1))
        key = flat.tobytes()
        cached = self._variance_cache.get(key)
        if cached is not None:
            self._variance_cache.move_to_end(key)
            return cached.reshape(values.shape)
        k = np.exp(flat)
        target = self.calls(k)
        intrinsic = np.maximum(1.0 - k, 0.0)
        if (
            not np.isfinite(target).all()
            or np.any(target <= intrinsic)
            or np.any(target >= 1.0)
        ):
            raise ValueError("TTF convex call core has no finite positive Black variance")
        low = np.zeros_like(k)
        high = np.full_like(k, max(64.0, 4.0 * self.variance_nodes.max()))
        for _ in range(12):
            missing = _normalized_calls(k, high) < target
            if not missing.any():
                break
            high[missing] *= 2.0
        else:
            raise ValueError("TTF convex call core could not bracket Black variance")
        for _ in range(70):
            middle = (low + high) * 0.5
            below = _normalized_calls(k, middle) < target
            low = np.where(below, middle, low)
            high = np.where(below, high, middle)
        variance = (low + high) * 0.5
        for node, anchor in zip(self.x_nodes, self.variance_nodes):
            variance[flat == node] = anchor
        variance.setflags(write=False)
        self._variance_cache[key] = variance
        if len(self._variance_cache) > 8:
            self._variance_cache.popitem(last=False)
        return variance.reshape(values.shape)

    def derivative(self, nu=1):
        if nu != 1:
            raise ValueError("Convex call core exposes the first variance derivative only")

        def evaluate(x):
            values = np.asarray(x, dtype=float)
            k = np.exp(values)
            variance = self(values)
            root = np.sqrt(variance)
            d1 = -values / root + 0.5 * root
            d2 = d1 - root
            vega_variance = _normal_pdf(d1) / (2.0 * root)
            result = k * (self.calls.derivative()(k) + ndtr(d2)) / vega_variance
            if not np.isfinite(result).all():
                raise ValueError("TTF convex call core has non-finite boundary derivatives")
            return result

        return evaluate

    def density(self, x):
        values = np.asarray(x, dtype=float)
        k = np.exp(values)
        variance = self(values)
        root = np.sqrt(variance)
        d2 = -values / root - 0.5 * root
        return self.calls.derivative(2)(k) * k * root / _normal_pdf(d2)


def build_convex_call_core(x_nodes, variance_nodes, variance_interpolator, *, margin):
    """Fit a decreasing, strictly convex interpolant with fixed anchor prices."""
    x = np.asarray(x_nodes, dtype=float)
    variance = np.asarray(variance_nodes, dtype=float)
    k = np.exp(x)
    calls = _normalized_calls(k, variance)
    widths = np.diff(k)
    secants = np.diff(calls) / widths
    if (
        not np.isfinite(calls).all()
        or np.any(calls <= np.maximum(1.0 - k, 0.0))
        or np.any(calls >= 1.0)
        or np.any(secants <= -1.0)
        or np.any(secants >= 0.0)
        or np.any(np.diff(secants) <= 0.0)
    ):
        raise ValueError("TTF anchors do not admit the governed convex call-price fallback")
    root = np.sqrt(variance)
    d1 = -x / root + 0.5 * root
    d2 = d1 - root
    initial = -ndtr(d2) + (
        _normal_pdf(d1) / (2.0 * root)
        * variance_interpolator.derivative()(x) / k
    )
    # Preserve the original core's endpoint variance tangents so an interior
    # repair does not unnecessarily change the established Wing join geometry.
    derivative_bounds = [(-1.0, 0.0)] * len(k)
    for index in (0, len(k) - 1):
        if not -1.0 <= initial[index] <= 0.0:
            raise ValueError("TTF core boundary tangent violates call-price monotonicity")
        derivative_bounds[index] = (initial[index], initial[index])
    constraints = []
    lower_bounds = []
    for index, width in enumerate(widths):
        sample_k = np.linspace(k[index], k[index + 1], 101)
        sample_w = variance_interpolator(np.log(sample_k))
        root = np.sqrt(sample_w)
        d2 = -np.log(sample_k) / root - 0.5 * root
        # Density g = call_gamma * K/F * sqrt(w) / phi(d2).
        # A conservative starting gamma floor is followed by an independent
        # complete core density check in the caller and the existing hybrid gate.
        gamma_floor = 2.0 * margin * np.max(_normal_pdf(d2) / (sample_k * root))
        row = np.zeros(len(k))
        row[index:index + 2] = [-4.0, -2.0]
        constraints.append(row)
        lower_bounds.append(width * gamma_floor - 6.0 * secants[index])
        row = np.zeros(len(k))
        row[index:index + 2] = [2.0, 4.0]
        constraints.append(row)
        lower_bounds.append(width * gamma_floor + 6.0 * secants[index])
    matrix = np.asarray(constraints)
    lower = np.asarray(lower_bounds)
    fitted = minimize(
        lambda derivatives: float(np.sum((derivatives - initial)**2)),
        initial,
        jac=lambda derivatives: 2.0 * (derivatives - initial),
        method="SLSQP",
        bounds=derivative_bounds,
        constraints=[LinearConstraint(matrix, lower, np.inf)],
        options={"ftol": 1e-13, "maxiter": 1000},
    )
    if (
        not fitted.success
        or not np.isfinite(fitted.x).all()
        or np.min(matrix @ fitted.x - lower) < -1e-10
        or np.any(fitted.x < -1.0)
        or np.any(fitted.x > 0.0)
    ):
        raise ValueError("TTF quote-preserving convex core fit failed its price constraints")
    core = ConvexCallTotalVariance(x, variance, calls, fitted.x)
    return core, {
        "solver_success": bool(fitted.success),
        "iterations": int(fitted.nit),
        "minimum_price_constraint_slack": float(np.min(matrix @ fitted.x - lower)),
        "anchor_prices_preserved": True,
        "original_boundary_tangents_preserved": True,
    }
