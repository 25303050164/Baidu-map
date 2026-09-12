import math

RADIUS = 6371008.8


def normalize(point):
    if len(point) != 2 or not all(math.isfinite(v) for v in point):
        raise ValueError("坐标必须为有限的经纬度")
    if not (-180 <= point[0] <= 180 and -90 <= point[1] <= 90):
        raise ValueError("坐标越界")
    return tuple(round(float(v), 6) for v in point)


class LocalProjection:
    """Local sampling scale only: this is NOT a BD09 -> WGS84 conversion."""

    def __init__(self, origin):
        self.origin = origin
        self.sx = RADIUS * math.cos(math.radians(origin[1])) * math.pi / 180
        self.sy = RADIUS * math.pi / 180

    def to_local(self, point):
        return ((point[0] - self.origin[0]) * self.sx, (point[1] - self.origin[1]) * self.sy)

    def to_geographic(self, point):
        return (point[0] / self.sx + self.origin[0], point[1] / self.sy + self.origin[1])
