"""The distances every checkup stage derives from the one service rule.

The business rule is "a facility within 1000 m on foot, with a 100 m tolerance
band"; entrances attach to the walking network within 50 m. Every other distance
is derived here, so no stage carries a constant unrelated to the rule.
"""

#: The service rule: walking-route distance, inclusive, and its tolerance band.
THRESHOLD_M = 1000.0
TOLERANCE_M = 100.0
#: How far a service search runs: the far edge of the tolerance band.
SEARCH_CUTOFF_M = THRESHOLD_M + TOLERANCE_M
#: How far a facility entrance or a support point may attach to the network.
ENTRANCE_LIMIT_M = 50.0
#: Slack for the bd09/metric conversion and rounded coordinates.
CONVERSION_MARGIN_M = 150.0
#: The facility query range around the boundary: any facility a search can reach,
#: through an entrance up to the limit, plus the conversion slack.
QUERY_PADDING_M = SEARCH_CUTOFF_M + ENTRANCE_LIMIT_M + CONVERSION_MARGIN_M
#: Water is loaded this far around the assessment domain: every entrance a search
#: can reach needs its connection checked against it.
OBSTACLE_MARGIN_M = SEARCH_CUTOFF_M + ENTRANCE_LIMIT_M
#: A route endpoint may sit this far from the requested point and still be read
#: (the endpoint-tolerance evidence layer): the same limit an entrance has.
ENDPOINT_TOLERANCE_M = ENTRANCE_LIMIT_M
#: A facility further than this in a straight line from the centre cannot be
#: within the cutoff on foot, even with an entrance offset at both ends.
CANDIDATE_STRAIGHT_LINE_M = SEARCH_CUTOFF_M + 2 * ENTRANCE_LIMIT_M
