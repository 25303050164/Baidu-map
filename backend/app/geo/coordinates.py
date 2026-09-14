"""Approximate China coordinate conversions. All points are (longitude, latitude).

GCJ inverse is iterated; this is not survey-grade or an official exact transform.
The existing life_circle LocalProjection is a BD09 sampling scale, not a converter.
"""
import math


def _check(lng, lat):
    if not (math.isfinite(lng) and math.isfinite(lat) and -180 <= lng <= 180 and -90 <= lat <= 90):
        raise ValueError("invalid_coordinate")


def wgs84_to_gcj02(lng, lat):
    _check(lng, lat)
    if not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271):
        return lng, lat
    x, y = lng - 105, lat - 35
    dlat = -100 + 2*x + 3*y + 0.2*y*y + 0.1*x*y + 0.2*math.sqrt(abs(x))
    dlng = 300 + x + 2*y + 0.1*x*x + 0.1*x*y + 0.1*math.sqrt(abs(x))
    wave = (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    dlat += wave + (20*math.sin(y*math.pi) + 40*math.sin(y*math.pi/3))*2/3
    dlat += (160*math.sin(y*math.pi/12) + 320*math.sin(y*math.pi/30))*2/3
    dlng += wave + (20*math.sin(x*math.pi) + 40*math.sin(x*math.pi/3))*2/3
    dlng += (150*math.sin(x*math.pi/12) + 300*math.sin(x*math.pi/30))*2/3
    rad = math.radians(lat)
    magic = 1 - 0.00669342162296594323 * math.sin(rad)**2
    dlat = dlat*180 / ((6378245*(1-0.00669342162296594323))/(magic*math.sqrt(magic))*math.pi)
    dlng = dlng*180 / (6378245/math.sqrt(magic)*math.cos(rad)*math.pi)
    return lng + dlng, lat + dlat


def gcj02_to_wgs84(lng, lat):
    _check(lng, lat)
    guess = (lng, lat)
    for _ in range(10):
        forward = wgs84_to_gcj02(*guess)
        delta = (forward[0]-lng, forward[1]-lat)
        guess = (guess[0]-delta[0], guess[1]-delta[1])
        if max(map(abs, delta)) < 1e-10:
            break
    return guess


def gcj02_to_bd09(lng, lat):
    _check(lng, lat)
    k = math.pi*3000/180
    z = math.hypot(lng, lat) + 0.00002*math.sin(lat*k)
    theta = math.atan2(lat, lng) + 0.000003*math.cos(lng*k)
    return z*math.cos(theta)+0.0065, z*math.sin(theta)+0.006


def bd09_to_wgs84(lng, lat):
    _check(lng, lat)
    x, y = lng-0.0065, lat-0.006
    k = math.pi*3000/180
    z = math.hypot(x, y) - 0.00002*math.sin(y*k)
    theta = math.atan2(y, x) - 0.000003*math.cos(x*k)
    return gcj02_to_wgs84(z*math.cos(theta), z*math.sin(theta))


def wgs84_to_bd09(lng, lat):
    return gcj02_to_bd09(*wgs84_to_gcj02(lng, lat))
