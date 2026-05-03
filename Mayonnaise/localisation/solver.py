import math


def solve_from_distance_matrix(node_ids, dist_matrix):
    """
    Solve 2D node positions from a sparse distance matrix using classical MDS.

    node_ids:   ordered list of node ids
    dist_matrix: dict of (node_a, node_b) -> distance metres

    Returns dict of node_id -> (x, y, z).
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
            direct  = dist_matrix.get((node_a, node_b))
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

    squared   = [[distances[r][c] ** 2 for c in range(count)] for r in range(count)]
    row_means = [sum(squared[r]) / count for r in range(count)]
    col_means = [sum(squared[r][c] for r in range(count)) / count for c in range(count)]
    grand     = sum(row_means) / count

    centred = [
        [-0.5 * (squared[r][c] - row_means[r] - col_means[c] + grand) for c in range(count)]
        for r in range(count)
    ]

    def matvec(m, v):
        return [sum(m[r][c] * v[c] for c in range(count)) for r in range(count)]

    def dot(a, b):
        return sum(a[i] * b[i] for i in range(count))

    def normalise(v):
        mag = math.sqrt(sum(x * x for x in v))
        if mag <= 1e-10:
            return v, 0.0
        return [x / mag for x in v], mag

    v1 = [1.0 / math.sqrt(count)] * count
    for _ in range(150):
        v1 = matvec(centred, v1)
        v1, _ = normalise(v1)
    lam1 = dot(v1, matvec(centred, v1))

    deflated = [
        [centred[r][c] - lam1 * v1[r] * v1[c] for c in range(count)]
        for r in range(count)
    ]

    v2 = [(-1.0 if i % 2 == 0 else 1.0) for i in range(count)]
    v2, _ = normalise(v2)
    for _ in range(150):
        v2 = matvec(deflated, v2)
        proj = dot(v2, v1)
        v2 = [v2[i] - proj * v1[i] for i in range(count)]
        v2, _ = normalise(v2)
    lam2 = dot(v2, matvec(centred, v2))

    s1 = math.sqrt(max(lam1, 0.0))
    s2 = math.sqrt(max(lam2, 0.0))

    coords = {node_ids[i]: (v1[i] * s1, v2[i] * s2, 0.0) for i in range(count)}

    ox, oy, _ = coords[node_ids[0]]
    for nid, (x, y, _) in list(coords.items()):
        coords[nid] = (x - ox, y - oy, 0.0)

    ref = node_ids[1]
    rx, ry, _ = coords[ref]
    angle = math.atan2(ry, rx)
    ca, sa = math.cos(-angle), math.sin(-angle)
    for nid, (x, y, _) in list(coords.items()):
        coords[nid] = (x * ca - y * sa, x * sa + y * ca, 0.0)

    if coords[ref][0] < 0:
        for nid, (x, y, _) in list(coords.items()):
            coords[nid] = (-x, y, 0.0)

    neg_y = sum(1 for _, y, _ in coords.values() if y < 0)
    if neg_y > count // 2:
        for nid, (x, y, _) in list(coords.items()):
            coords[nid] = (x, -y, 0.0)

    return coords


def solve_position(measurements, current_pos=None):
    """
    Solve one node's position from known anchor coordinates + distances.

    measurements: list of ((x, y, z), distance_m)
    Returns (x, y, z) or None.
    """
    measurements = sorted(measurements, key=lambda m: m[1])
    count = len(measurements)
    if count == 0:
        return None
    if count == 1:
        anchor, dist = measurements[0]
        return (anchor[0] + dist, anchor[1], 0.0)
    if count == 2:
        (aa, da), (ab, db) = measurements
        result = _circle_intersect((aa[0], aa[1]), da, (ab[0], ab[1]), db, True)
        if result is None:
            return (aa[0] + da, aa[1], 0.0)
        if current_pos is not None:
            alt = _circle_intersect((aa[0], aa[1]), da, (ab[0], ab[1]), db, False)
            if alt is not None:
                def d2(a, b):
                    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2)
                if d2(alt, current_pos[:2]) < d2(result, current_pos[:2]):
                    result = alt
        return (result[0], result[1], 0.0)
    if count == 3:
        (aa, da), (ab, db), (ac, dc) = measurements[:3]
        result = _trilaterate_2d((aa[0], aa[1]), da, (ab[0], ab[1]), db, (ac[0], ac[1]), dc)
        if result is None:
            return solve_position(measurements[:2], current_pos=current_pos)
        return (result[0], result[1], 0.0)

    a0, d0 = measurements[0]
    x0, y0, z0 = a0
    matrix, rhs = [], []
    for ai, di in measurements[1:]:
        xi, yi, zi = ai
        matrix.append([2*(xi-x0), 2*(yi-y0), 2*(zi-z0)])
        rhs.append(d0*d0 - di*di - x0*x0 + xi*xi - y0*y0 + yi*yi - z0*z0 + zi*zi)

    ata = [[0.0]*3 for _ in range(3)]
    atb = [0.0]*3
    for row, rv in zip(matrix, rhs):
        for l in range(3):
            atb[l] += row[l] * rv
            for r in range(3):
                ata[l][r] += row[l] * row[r]

    def det3(m):
        return (m[0][0]*(m[1][1]*m[2][2]-m[1][2]*m[2][1])
               -m[0][1]*(m[1][0]*m[2][2]-m[1][2]*m[2][0])
               +m[0][2]*(m[1][0]*m[2][1]-m[1][1]*m[2][0]))

    def replaced(m, col, vec):
        c = [row[:] for row in m]
        for i in range(3):
            c[i][col] = vec[i]
        return c

    det = det3(ata)
    if abs(det) < 1e-9:
        return solve_position(measurements[:3], current_pos=current_pos)

    return (det3(replaced(ata, 0, atb))/det,
            det3(replaced(ata, 1, atb))/det,
            det3(replaced(ata, 2, atb))/det)


def _circle_intersect(pa, da, pb, db, prefer_positive_y=True):
    xa, ya = pa
    xb, yb = pb
    dx, dy = xb - xa, yb - ya
    span = math.sqrt(dx*dx + dy*dy)
    if span < 1e-6:
        return None
    da = min(da, span + db - 1e-6)
    db = min(db, span + da - 1e-6)
    da = max(da, abs(span - db) + 1e-6)
    along = (da*da - db*db + span*span) / (2*span)
    h2 = max(da*da - along*along, 0.0)
    h = math.sqrt(h2)
    mx = xa + along * dx / span
    my = ya + along * dy / span
    opt_a = (mx + h*dy/span, my - h*dx/span)
    opt_b = (mx - h*dy/span, my + h*dx/span)
    if prefer_positive_y:
        return opt_b if opt_b[1] >= opt_a[1] else opt_a
    return opt_a if opt_a[1] >= opt_b[1] else opt_b


def _trilaterate_2d(pa, da, pb, db, pc, dc):
    xa, ya = pa
    xb, yb = pb
    xc, yc = pc
    ca = 2*(xb-xa); cb = 2*(yb-ya)
    ra = da*da - db*db - xa*xa + xb*xb - ya*ya + yb*yb
    cc = 2*(xc-xa); cd = 2*(yc-ya)
    rb = da*da - dc*dc - xa*xa + xc*xc - ya*ya + yc*yc
    denom = ca*cd - cb*cc
    if abs(denom) < 1e-6:
        return None
    return ((ra*cd - rb*cb)/denom, (ca*rb - cc*ra)/denom)
