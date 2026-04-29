import math


def solve_from_distance_matrix(node_ids, dist_matrix):
    """
    Solve 2D node positions from a sparse distance matrix using classical MDS.

    node_ids: ordered list of node ids
    dist_matrix: dict of (node_a, node_b) -> distance metres
    """
    count = len(node_ids)
    if count == 0:
        return {}
    if count == 1:
        return {node_ids[0]: (0.0, 0.0, 0.0)}

    distances = [[0.0] * count for _ in range(count)]
    for row, node_a in enumerate(node_ids):
        for col, node_b in enumerate(node_ids):
            if row == col:
                continue

            direct = dist_matrix.get((node_a, node_b))
            reverse = dist_matrix.get((node_b, node_a))
            if direct and reverse:
                distances[row][col] = (direct + reverse) / 2.0
                continue
            if direct:
                distances[row][col] = direct
                continue
            if reverse:
                distances[row][col] = reverse
                continue

            indirect = []
            for pivot_index, pivot in enumerate(node_ids):
                if pivot_index == row or pivot_index == col:
                    continue
                leg_a = dist_matrix.get((node_a, pivot)) or dist_matrix.get((pivot, node_a))
                leg_b = dist_matrix.get((pivot, node_b)) or dist_matrix.get((node_b, pivot))
                if leg_a and leg_b:
                    indirect.append(leg_a + leg_b)

            distances[row][col] = min(indirect) if indirect else 1.0

    if count == 2:
        distance = distances[0][1]
        return {
            node_ids[0]: (0.0, 0.0, 0.0),
            node_ids[1]: (distance, 0.0, 0.0),
        }

    squared = [[distances[row][col] ** 2 for col in range(count)] for row in range(count)]
    row_means = [sum(squared[row]) / count for row in range(count)]
    col_means = [sum(squared[row][col] for row in range(count)) / count for col in range(count)]
    grand_mean = sum(row_means) / count

    centred = [
        [
            -0.5 * (squared[row][col] - row_means[row] - col_means[col] + grand_mean)
            for col in range(count)
        ]
        for row in range(count)
    ]

    def matvec(matrix, vector):
        return [sum(matrix[row][col] * vector[col] for col in range(count)) for row in range(count)]

    def dot(left, right):
        return sum(left[index] * right[index] for index in range(count))

    def normalise(vector):
        magnitude = math.sqrt(sum(value * value for value in vector))
        if magnitude <= 1e-10:
            return vector, 0.0
        return [value / magnitude for value in vector], magnitude

    first = [1.0 / math.sqrt(count)] * count
    for _ in range(150):
        first = matvec(centred, first)
        first, _ = normalise(first)
    lambda_one = dot(first, matvec(centred, first))

    deflated = [
        [centred[row][col] - lambda_one * first[row] * first[col] for col in range(count)]
        for row in range(count)
    ]

    second = [(-1.0 if index % 2 == 0 else 1.0) for index in range(count)]
    second, _ = normalise(second)
    for _ in range(150):
        second = matvec(deflated, second)
        projection = dot(second, first)
        second = [second[index] - projection * first[index] for index in range(count)]
        second, _ = normalise(second)
    lambda_two = dot(second, matvec(centred, second))

    scale_one = math.sqrt(max(lambda_one, 0.0))
    scale_two = math.sqrt(max(lambda_two, 0.0))

    coords = {}
    for index, node_id in enumerate(node_ids):
        coords[node_id] = (first[index] * scale_one, second[index] * scale_two, 0.0)

    origin_x, origin_y, _ = coords[node_ids[0]]
    for node_id, (x_pos, y_pos, _) in list(coords.items()):
        coords[node_id] = (x_pos - origin_x, y_pos - origin_y, 0.0)

    ref_id = node_ids[1]
    ref_x, ref_y, _ = coords[ref_id]
    angle = math.atan2(ref_y, ref_x)
    cos_angle = math.cos(-angle)
    sin_angle = math.sin(-angle)
    for node_id, (x_pos, y_pos, _) in list(coords.items()):
        coords[node_id] = (
            x_pos * cos_angle - y_pos * sin_angle,
            x_pos * sin_angle + y_pos * cos_angle,
            0.0,
        )

    if coords[ref_id][0] < 0:
        for node_id, (x_pos, y_pos, _) in list(coords.items()):
            coords[node_id] = (-x_pos, y_pos, 0.0)

    negative_y = 0
    for _, y_pos, _ in coords.values():
        if y_pos < 0:
            negative_y += 1
    if negative_y > count // 2:
        for node_id, (x_pos, y_pos, _) in list(coords.items()):
            coords[node_id] = (x_pos, -y_pos, 0.0)

    return coords


def _circle_intersect(point_a, dist_a, point_b, dist_b, prefer_positive_y=True):
    x_a, y_a = point_a[0], point_a[1]
    x_b, y_b = point_b[0], point_b[1]
    dx = x_b - x_a
    dy = y_b - y_a
    span = math.sqrt(dx * dx + dy * dy)
    if span < 1e-6:
        return None

    dist_a = min(dist_a, span + dist_b - 1e-6)
    dist_b = min(dist_b, span + dist_a - 1e-6)
    dist_a = max(dist_a, abs(span - dist_b) + 1e-6)

    along = (dist_a * dist_a - dist_b * dist_b + span * span) / (2 * span)
    height_sq = dist_a * dist_a - along * along
    if height_sq < 0:
        height_sq = 0.0
    height = math.sqrt(height_sq)

    mid_x = x_a + along * dx / span
    mid_y = y_a + along * dy / span
    option_a = (mid_x + height * dy / span, mid_y - height * dx / span)
    option_b = (mid_x - height * dy / span, mid_y + height * dx / span)

    if prefer_positive_y:
        return option_b if option_b[1] >= option_a[1] else option_a
    return option_a if option_a[1] >= option_b[1] else option_b


def _trilaterate_2d(point_a, dist_a, point_b, dist_b, point_c, dist_c):
    x_a, y_a = point_a
    x_b, y_b = point_b
    x_c, y_c = point_c

    coeff_a = 2 * (x_b - x_a)
    coeff_b = 2 * (y_b - y_a)
    rhs_a = dist_a * dist_a - dist_b * dist_b - x_a * x_a + x_b * x_b - y_a * y_a + y_b * y_b

    coeff_c = 2 * (x_c - x_a)
    coeff_d = 2 * (y_c - y_a)
    rhs_b = dist_a * dist_a - dist_c * dist_c - x_a * x_a + x_c * x_c - y_a * y_a + y_c * y_c

    denom = coeff_a * coeff_d - coeff_b * coeff_c
    if abs(denom) < 1e-6:
        return None

    return (
        (rhs_a * coeff_d - rhs_b * coeff_b) / denom,
        (coeff_a * rhs_b - coeff_c * rhs_a) / denom,
    )


def solve_position(measurements, current_pos=None):
    """
    Solve one node position from known anchor coordinates plus distances.

    measurements: list of ((x, y, z), distance_m)
    """
    measurements = sorted(measurements, key=lambda item: item[1])
    count = len(measurements)
    if count == 0:
        return None

    if count == 1:
        anchor, distance = measurements[0]
        return (anchor[0] + distance, anchor[1], 0.0)

    if count == 2:
        (anchor_a, dist_a), (anchor_b, dist_b) = measurements
        result = _circle_intersect(
            (anchor_a[0], anchor_a[1]),
            dist_a,
            (anchor_b[0], anchor_b[1]),
            dist_b,
            True,
        )
        if result is None:
            return (anchor_a[0] + dist_a, anchor_a[1], 0.0)

        if current_pos is not None:
            alt = _circle_intersect(
                (anchor_a[0], anchor_a[1]),
                dist_a,
                (anchor_b[0], anchor_b[1]),
                dist_b,
                False,
            )
            if alt is not None:
                def distance_2d(point_a, point_b):
                    return math.sqrt(
                        (point_a[0] - point_b[0]) * (point_a[0] - point_b[0]) +
                        (point_a[1] - point_b[1]) * (point_a[1] - point_b[1])
                    )

                if distance_2d(alt, current_pos[:2]) < distance_2d(result, current_pos[:2]):
                    result = alt

        return (result[0], result[1], 0.0)

    if count == 3:
        (anchor_a, dist_a), (anchor_b, dist_b), (anchor_c, dist_c) = measurements[:3]
        result = _trilaterate_2d(
            (anchor_a[0], anchor_a[1]),
            dist_a,
            (anchor_b[0], anchor_b[1]),
            dist_b,
            (anchor_c[0], anchor_c[1]),
            dist_c,
        )
        if result is None:
            return solve_position(measurements[:2], current_pos=current_pos)
        return (result[0], result[1], 0.0)

    anchor_0, dist_0 = measurements[0]
    x_0, y_0, z_0 = anchor_0
    matrix = []
    rhs = []
    for anchor_i, dist_i in measurements[1:]:
        x_i, y_i, z_i = anchor_i
        matrix.append([2 * (x_i - x_0), 2 * (y_i - y_0), 2 * (z_i - z_0)])
        rhs.append(
            dist_0 * dist_0 - dist_i * dist_i
            - x_0 * x_0 + x_i * x_i
            - y_0 * y_0 + y_i * y_i
            - z_0 * z_0 + z_i * z_i
        )

    ata = [[0.0] * 3 for _ in range(3)]
    atb = [0.0] * 3
    for row, rhs_value in zip(matrix, rhs):
        for left in range(3):
            atb[left] += row[left] * rhs_value
            for right in range(3):
                ata[left][right] += row[left] * row[right]

    def det3(matrix3):
        return (
            matrix3[0][0] * (matrix3[1][1] * matrix3[2][2] - matrix3[1][2] * matrix3[2][1])
            - matrix3[0][1] * (matrix3[1][0] * matrix3[2][2] - matrix3[1][2] * matrix3[2][0])
            + matrix3[0][2] * (matrix3[1][0] * matrix3[2][1] - matrix3[1][1] * matrix3[2][0])
        )

    def replaced(matrix3, column, vector):
        clone = [row[:] for row in matrix3]
        for index in range(3):
            clone[index][column] = vector[index]
        return clone

    determinant = det3(ata)
    if abs(determinant) < 1e-9:
        return solve_position(measurements[:3], current_pos=current_pos)

    return (
        det3(replaced(ata, 0, atb)) / determinant,
        det3(replaced(ata, 1, atb)) / determinant,
        det3(replaced(ata, 2, atb)) / determinant,
    )
