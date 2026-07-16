from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


Point = tuple[float, float]


@dataclass(frozen=True)
class Cubic:
    start: Point
    control1: Point
    control2: Point
    end: Point


def _normalize(vector: np.ndarray) -> np.ndarray:
    length = float(np.linalg.norm(vector))
    if length <= 1e-12:
        return np.zeros(2, dtype=np.float64)
    return vector / length


def _bezier(cubic: np.ndarray, t: float) -> np.ndarray:
    u = 1 - t
    return (
        cubic[0] * u**3
        + cubic[1] * 3 * u**2 * t
        + cubic[2] * 3 * u * t**2
        + cubic[3] * t**3
    )


def _bezier_first(cubic: np.ndarray, t: float) -> np.ndarray:
    u = 1 - t
    return (
        (cubic[1] - cubic[0]) * 3 * u**2
        + (cubic[2] - cubic[1]) * 6 * u * t
        + (cubic[3] - cubic[2]) * 3 * t**2
    )


def _bezier_second(cubic: np.ndarray, t: float) -> np.ndarray:
    return (
        (cubic[2] - 2 * cubic[1] + cubic[0]) * 6 * (1 - t)
        + (cubic[3] - 2 * cubic[2] + cubic[1]) * 6 * t
    )


def _chord_parameters(points: np.ndarray) -> np.ndarray:
    distances = np.linalg.norm(np.diff(points, axis=0), axis=1)
    parameters = np.concatenate(([0.0], np.cumsum(distances)))
    if parameters[-1] <= 1e-12:
        return np.linspace(0, 1, len(points))
    return parameters / parameters[-1]


def _generate_bezier(
    points: np.ndarray,
    parameters: np.ndarray,
    left_tangent: np.ndarray,
    right_tangent: np.ndarray,
) -> np.ndarray:
    start = points[0]
    end = points[-1]
    c00 = c01 = c11 = x0 = x1 = 0.0
    for point, parameter in zip(points, parameters, strict=True):
        u = 1 - parameter
        b0 = u**3
        b1 = 3 * parameter * u**2
        b2 = 3 * parameter**2 * u
        b3 = parameter**3
        a0 = left_tangent * b1
        a1 = right_tangent * b2
        residual = point - (start * (b0 + b1) + end * (b2 + b3))
        c00 += float(np.dot(a0, a0))
        c01 += float(np.dot(a0, a1))
        c11 += float(np.dot(a1, a1))
        x0 += float(np.dot(a0, residual))
        x1 += float(np.dot(a1, residual))
    determinant = c00 * c11 - c01 * c01
    segment_length = float(np.linalg.norm(end - start))
    epsilon = 1e-6 * segment_length
    if abs(determinant) > 1e-12:
        alpha_left = (x0 * c11 - x1 * c01) / determinant
        alpha_right = (c00 * x1 - c01 * x0) / determinant
    else:
        alpha_left = alpha_right = segment_length / 3
    if alpha_left < epsilon or alpha_right < epsilon:
        alpha_left = alpha_right = segment_length / 3
    return np.asarray(
        [
            start,
            start + left_tangent * alpha_left,
            end + right_tangent * alpha_right,
            end,
        ],
        dtype=np.float64,
    )


def _max_error(
    points: np.ndarray,
    cubic: np.ndarray,
    parameters: np.ndarray,
) -> tuple[float, int]:
    split = len(points) // 2
    maximum = 0.0
    for index in range(1, len(points) - 1):
        delta = _bezier(cubic, float(parameters[index])) - points[index]
        distance = float(np.dot(delta, delta))
        if distance >= maximum:
            maximum = distance
            split = index
    return maximum, split


def _newton_parameter(cubic: np.ndarray, point: np.ndarray, parameter: float) -> float:
    value = _bezier(cubic, parameter)
    first = _bezier_first(cubic, parameter)
    second = _bezier_second(cubic, parameter)
    difference = value - point
    denominator = float(np.dot(first, first) + np.dot(difference, second))
    if abs(denominator) <= 1e-12:
        return parameter
    return max(0.0, min(1.0, parameter - float(np.dot(difference, first)) / denominator))


def _fit_cubic(
    points: np.ndarray,
    left_tangent: np.ndarray,
    right_tangent: np.ndarray,
    error_squared: float,
    output: list[np.ndarray],
    *,
    depth: int = 0,
) -> None:
    if len(points) == 2:
        distance = float(np.linalg.norm(points[1] - points[0])) / 3
        output.append(
            np.asarray(
                [
                    points[0],
                    points[0] + left_tangent * distance,
                    points[1] + right_tangent * distance,
                    points[1],
                ]
            )
        )
        return
    parameters = _chord_parameters(points)
    cubic = _generate_bezier(points, parameters, left_tangent, right_tangent)
    maximum, split = _max_error(points, cubic, parameters)
    if maximum <= error_squared:
        output.append(cubic)
        return
    if maximum <= error_squared * 4:
        for _ in range(5):
            parameters = np.asarray(
                [
                    _newton_parameter(cubic, point, float(parameter))
                    for point, parameter in zip(points, parameters, strict=True)
                ]
            )
            if np.any(np.diff(parameters) <= 0):
                break
            cubic = _generate_bezier(points, parameters, left_tangent, right_tangent)
            maximum, split = _max_error(points, cubic, parameters)
            if maximum <= error_squared:
                output.append(cubic)
                return
    if depth > 64 or split <= 0 or split >= len(points) - 1:
        split = max(1, min(len(points) - 2, len(points) // 2))
    center_tangent = _normalize(points[split - 1] - points[split + 1])
    if not np.any(center_tangent):
        center_tangent = _normalize(points[split] - points[split + 1])
    _fit_cubic(
        points[: split + 1],
        left_tangent,
        center_tangent,
        error_squared,
        output,
        depth=depth + 1,
    )
    _fit_cubic(
        points[split:],
        -center_tangent,
        right_tangent,
        error_squared,
        output,
        depth=depth + 1,
    )


def fit_open_curve(points: list[Point], error: float) -> list[Cubic]:
    if len(points) < 2:
        return []
    array = np.asarray(points, dtype=np.float64)
    left_tangent = _normalize(array[1] - array[0])
    right_tangent = _normalize(array[-2] - array[-1])
    output: list[np.ndarray] = []
    _fit_cubic(array, left_tangent, right_tangent, error * error, output)
    return [
        Cubic(
            start=tuple(float(value) for value in cubic[0]),
            control1=tuple(float(value) for value in cubic[1]),
            control2=tuple(float(value) for value in cubic[2]),
            end=tuple(float(value) for value in cubic[3]),
        )
        for cubic in output
    ]


def _corner_indices(points: list[Point], angle_threshold: float) -> list[int]:
    corners: list[int] = []
    for index, point in enumerate(points):
        previous = np.asarray(points[index - 1], dtype=np.float64)
        current = np.asarray(point, dtype=np.float64)
        following = np.asarray(points[(index + 1) % len(points)], dtype=np.float64)
        left = _normalize(previous - current)
        right = _normalize(following - current)
        if not np.any(left) or not np.any(right):
            continue
        angle = math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(left, right))))))
        if angle <= angle_threshold:
            corners.append(index)
    return corners


def fit_closed_curve(
    points: list[Point],
    error: float,
    *,
    corner_angle: float = 125,
) -> list[Cubic]:
    if len(points) < 3:
        return []
    corners = _corner_indices(points, corner_angle)
    if len(corners) < 2:
        anchor = 0
        opposite = max(
            range(1, len(points)),
            key=lambda index: (
                (points[index][0] - points[anchor][0]) ** 2
                + (points[index][1] - points[anchor][1]) ** 2
            ),
        )
        corners = sorted((anchor, opposite))
    curves: list[Cubic] = []
    for offset, start in enumerate(corners):
        end = corners[(offset + 1) % len(corners)]
        if end > start:
            segment = points[start : end + 1]
        else:
            segment = points[start:] + points[: end + 1]
        if len(segment) < 2:
            continue
        curves.extend(fit_open_curve(segment, error))
    return curves


__all__ = ["Cubic", "fit_closed_curve", "fit_open_curve"]

