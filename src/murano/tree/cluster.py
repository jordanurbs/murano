"""Tiny from-scratch k-means with k-means++ initialization (numpy only).

Used by the summary-tree builder. Cosine similarity is implemented by
L2-normalizing inputs and using ordinary euclidean k-means — on the unit
sphere, smaller euclidean distance is equivalent to higher cosine similarity.

For the cluster sizes we care about (up to a few thousand chunks), this is
plenty fast and saves us a 30 MB sklearn dependency.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DEFAULT_MAX_ITER = 50
DEFAULT_N_INIT = 4  # How many random restarts to try; best result wins.
DEFAULT_TOL = 1e-4


def l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """L2-normalize each row. Zero-norm rows become zero vectors (won't bias k-means)."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return matrix / norms


def recommend_k(n_items: int) -> int:
    """k = round(sqrt(N)), clamped to [2, N//2]. Returns 0 if N <= 4 (no point clustering)."""
    if n_items < 5:
        return 0
    k = int(round(np.sqrt(n_items)))
    return max(2, min(n_items // 2, k))


@dataclass
class ClusterResult:
    labels: np.ndarray  # shape (n,), int, values in [0, k)
    centroids: np.ndarray  # shape (k, dim), L2-normalized
    inertia: float  # sum of squared distances to assigned centroid


def _kmeanspp_init(X: np.ndarray, k: int, rng: np.random.Generator) -> np.ndarray:
    """k-means++ initial centroid selection."""
    n = X.shape[0]
    centroids = np.empty((k, X.shape[1]), dtype=X.dtype)
    first = int(rng.integers(0, n))
    centroids[0] = X[first]

    closest_sq = np.sum((X - centroids[0]) ** 2, axis=1)
    for i in range(1, k):
        total = float(closest_sq.sum())
        if total <= 0.0:
            pick = int(rng.integers(0, n))
        else:
            probs = closest_sq / total
            pick = int(rng.choice(n, p=probs))
        centroids[i] = X[pick]
        new_sq = np.sum((X - centroids[i]) ** 2, axis=1)
        closest_sq = np.minimum(closest_sq, new_sq)
    return centroids


def _single_run(
    X: np.ndarray, k: int, *, max_iter: int, tol: float, rng: np.random.Generator
) -> ClusterResult:
    centroids = _kmeanspp_init(X, k, rng)
    labels = np.zeros(X.shape[0], dtype=np.int32)
    prev_inertia = float("inf")
    inertia = prev_inertia

    for _ in range(max_iter):
        # Assignment: distance from each point to each centroid.
        # ||x - c||^2 = ||x||^2 + ||c||^2 - 2 x·c. For unit vectors, ||x||==||c||==1.
        # We compute generally so unnormalized inputs still work.
        d2 = (
            np.sum(X * X, axis=1, keepdims=True)
            + np.sum(centroids * centroids, axis=1)
            - 2 * X @ centroids.T
        )
        labels = np.argmin(d2, axis=1).astype(np.int32)
        inertia = float(d2[np.arange(X.shape[0]), labels].sum())

        # Update centroids.
        new_centroids = np.empty_like(centroids)
        for j in range(k):
            members = X[labels == j]
            if len(members) == 0:
                # Empty cluster — re-seed to the farthest point from any centroid.
                far_idx = int(np.argmax(d2.min(axis=1)))
                new_centroids[j] = X[far_idx]
            else:
                new_centroids[j] = members.mean(axis=0)

        # Re-normalize centroids to the unit sphere so cosine semantics hold.
        new_centroids = l2_normalize(new_centroids)

        shift = float(np.linalg.norm(new_centroids - centroids))
        centroids = new_centroids
        if abs(prev_inertia - inertia) < tol and shift < tol:
            break
        prev_inertia = inertia

    return ClusterResult(labels=labels, centroids=centroids, inertia=inertia)


def kmeans(
    embeddings: np.ndarray,
    k: int,
    *,
    max_iter: int = DEFAULT_MAX_ITER,
    n_init: int = DEFAULT_N_INIT,
    tol: float = DEFAULT_TOL,
    seed: int | None = None,
) -> ClusterResult:
    """K-means on L2-normalized inputs (cosine-equivalent). Returns the best of n_init runs."""
    if embeddings.ndim != 2:
        raise ValueError(f"embeddings must be 2-D; got shape {embeddings.shape}")
    n = embeddings.shape[0]
    if k <= 0:
        raise ValueError("k must be > 0")
    if k > n:
        raise ValueError(f"k ({k}) cannot exceed number of items ({n})")

    X = l2_normalize(embeddings.astype(np.float64))
    rng = np.random.default_rng(seed)
    best: ClusterResult | None = None
    for _ in range(n_init):
        result = _single_run(X, k, max_iter=max_iter, tol=tol, rng=rng)
        if best is None or result.inertia < best.inertia:
            best = result
    assert best is not None
    return best
