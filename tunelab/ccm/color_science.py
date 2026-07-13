from __future__ import annotations

import math

from .models import Matrix3, Vector3


SRGB_TO_XYZ: Matrix3 = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)

XYZ_TO_SRGB: Matrix3 = (
    (3.2404542, -1.5371385, -0.4985314),
    (-0.9692660, 1.8760108, 0.0415560),
    (0.0556434, -0.2040259, 1.0572252),
)

D65_WHITE: Vector3 = (0.95047, 1.0, 1.08883)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def mat_vec(matrix: Matrix3, vector: Vector3) -> Vector3:
    return tuple(sum(matrix[row][col] * vector[col] for col in range(3)) for row in range(3))  # type: ignore[return-value]


def mat_mul(left: Matrix3, right: Matrix3) -> Matrix3:
    return tuple(
        tuple(sum(left[row][k] * right[k][col] for k in range(3)) for col in range(3))
        for row in range(3)
    )  # type: ignore[return-value]


def identity_matrix() -> Matrix3:
    return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def matrix_blend(matrix: Matrix3, strength: float) -> Matrix3:
    identity = identity_matrix()
    return tuple(
        tuple(identity[row][col] + strength * (matrix[row][col] - identity[row][col]) for col in range(3))
        for row in range(3)
    )  # type: ignore[return-value]


def row_sums(matrix: Matrix3) -> Vector3:
    return tuple(sum(row) for row in matrix)  # type: ignore[return-value]


def srgb_channel_to_linear(value: float) -> float:
    value = clamp(value)
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def linear_channel_to_srgb(value: float, *, clip: bool = True) -> float:
    if value <= 0.0031308:
        result = 12.92 * value
    else:
        result = 1.055 * (max(value, 0.0) ** (1.0 / 2.4)) - 0.055
    return clamp(result) if clip else result


def srgb_to_linear(rgb: Vector3) -> Vector3:
    return tuple(srgb_channel_to_linear(value) for value in rgb)  # type: ignore[return-value]


def linear_to_srgb(rgb: Vector3, *, clip: bool = True) -> Vector3:
    return tuple(linear_channel_to_srgb(value, clip=clip) for value in rgb)  # type: ignore[return-value]


def linear_rgb_to_xyz(rgb: Vector3) -> Vector3:
    return mat_vec(SRGB_TO_XYZ, rgb)


def xyz_to_linear_rgb(xyz: Vector3) -> Vector3:
    return mat_vec(XYZ_TO_SRGB, xyz)


def srgb_to_lab(rgb: Vector3) -> Vector3:
    return xyz_to_lab(linear_rgb_to_xyz(srgb_to_linear(rgb)))


def lab_to_srgb(lab: Vector3, *, clip: bool = True) -> Vector3:
    return linear_to_srgb(xyz_to_linear_rgb(lab_to_xyz(lab)), clip=clip)


def xyz_to_lab(xyz: Vector3) -> Vector3:
    delta = 6.0 / 29.0

    def f(value: float) -> float:
        if value > delta**3:
            return value ** (1.0 / 3.0)
        return value / (3.0 * delta**2) + 4.0 / 29.0

    fx, fy, fz = (f(xyz[i] / D65_WHITE[i]) for i in range(3))
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


def lab_to_xyz(lab: Vector3) -> Vector3:
    lightness, a_value, b_value = lab
    fy = (lightness + 16.0) / 116.0
    fx = fy + a_value / 500.0
    fz = fy - b_value / 200.0
    delta = 6.0 / 29.0

    def f_inv(value: float) -> float:
        if value > delta:
            return value**3
        return 3.0 * delta**2 * (value - 4.0 / 29.0)

    return (
        D65_WHITE[0] * f_inv(fx),
        D65_WHITE[1] * f_inv(fy),
        D65_WHITE[2] * f_inv(fz),
    )


def lab_chroma(lab: Vector3) -> float:
    return math.hypot(lab[1], lab[2])


def hue_angle_degrees(lab: Vector3) -> float:
    angle = math.degrees(math.atan2(lab[2], lab[1]))
    return angle + 360.0 if angle < 0 else angle


def hue_difference_degrees(first: Vector3, second: Vector3) -> float:
    difference = hue_angle_degrees(first) - hue_angle_degrees(second)
    return (difference + 180.0) % 360.0 - 180.0


def delta_e_2000(lab1: Vector3, lab2: Vector3) -> float:
    """Return CIEDE2000 using the Sharma/Wu/Dalal reference equations."""

    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    c1 = math.hypot(a1, b1)
    c2 = math.hypot(a2, b2)
    c_bar = (c1 + c2) / 2.0
    c_bar7 = c_bar**7
    g = 0.5 * (1.0 - math.sqrt(c_bar7 / (c_bar7 + 25.0**7))) if c_bar else 0.5
    a1_prime = (1.0 + g) * a1
    a2_prime = (1.0 + g) * a2
    c1_prime = math.hypot(a1_prime, b1)
    c2_prime = math.hypot(a2_prime, b2)

    def hue(a_value: float, b_value: float) -> float:
        if abs(a_value) < 1e-15 and abs(b_value) < 1e-15:
            return 0.0
        result = math.degrees(math.atan2(b_value, a_value))
        return result + 360.0 if result < 0.0 else result

    h1_prime = hue(a1_prime, b1)
    h2_prime = hue(a2_prime, b2)
    delta_l_prime = l2 - l1
    delta_c_prime = c2_prime - c1_prime

    if c1_prime * c2_prime == 0.0:
        delta_h_prime = 0.0
    elif abs(h2_prime - h1_prime) <= 180.0:
        delta_h_prime = h2_prime - h1_prime
    elif h2_prime <= h1_prime:
        delta_h_prime = h2_prime - h1_prime + 360.0
    else:
        delta_h_prime = h2_prime - h1_prime - 360.0

    delta_big_h_prime = 2.0 * math.sqrt(c1_prime * c2_prime) * math.sin(math.radians(delta_h_prime / 2.0))
    l_bar_prime = (l1 + l2) / 2.0
    c_bar_prime = (c1_prime + c2_prime) / 2.0
    if c1_prime * c2_prime == 0.0:
        h_bar_prime = h1_prime + h2_prime
    elif abs(h1_prime - h2_prime) <= 180.0:
        h_bar_prime = (h1_prime + h2_prime) / 2.0
    elif h1_prime + h2_prime < 360.0:
        h_bar_prime = (h1_prime + h2_prime + 360.0) / 2.0
    else:
        h_bar_prime = (h1_prime + h2_prime - 360.0) / 2.0

    t_value = (
        1.0
        - 0.17 * math.cos(math.radians(h_bar_prime - 30.0))
        + 0.24 * math.cos(math.radians(2.0 * h_bar_prime))
        + 0.32 * math.cos(math.radians(3.0 * h_bar_prime + 6.0))
        - 0.20 * math.cos(math.radians(4.0 * h_bar_prime - 63.0))
    )
    delta_theta = 30.0 * math.exp(-((h_bar_prime - 275.0) / 25.0) ** 2)
    c_bar_prime7 = c_bar_prime**7
    r_c = 2.0 * math.sqrt(c_bar_prime7 / (c_bar_prime7 + 25.0**7)) if c_bar_prime else 0.0
    s_l = 1.0 + 0.015 * (l_bar_prime - 50.0) ** 2 / math.sqrt(20.0 + (l_bar_prime - 50.0) ** 2)
    s_c = 1.0 + 0.045 * c_bar_prime
    s_h = 1.0 + 0.015 * c_bar_prime * t_value
    r_t = -math.sin(math.radians(2.0 * delta_theta)) * r_c
    l_term = delta_l_prime / s_l
    c_term = delta_c_prime / s_c
    h_term = delta_big_h_prime / s_h
    return math.sqrt(l_term**2 + c_term**2 + h_term**2 + r_t * c_term * h_term)
