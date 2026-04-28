"""
localise.py — Full Distance Matrix Localisation
=================================================
Uses cooperative multilateration — every node ranges every other node,
giving a symmetric distance matrix. Positions solved from full matrix.

Algorithm:
  1. Build NxN distance matrix from mutual range reports
  2. Average reciprocal measurements (A→B and B→A) to reduce noise
  3. Use classical MDS (Multidimensional Scaling) to recover 2D positions
  4. Align coordinate frame: Node 0 at origin, Node 0→Node 1 = positive X

MDS is the standard algorithm for this exact problem — used in GPS ground
stations, robot swarms, and sensor network localisation.
"""

import math


def solve_from_distance_matrix(node_ids, dist_matrix):
    """
    Solve node positions from a full distance matrix using classical MDS.

    node_ids: ordered list of node IDs, e.g. [0, 1, 2, 3]
    dist_matrix: dict of (id_a, id_b) → distance_metres
                 Reciprocal pairs averaged automatically.

    Returns: dict of node_id → (x, y, z)
    Coordinate frame: Node 0 at origin, Node 1 on positive X axis.
    """
    n = len(node_ids)
    if n < 2:
        return {node_ids[0]: (0.0, 0.0, 0.0)} if n == 1 else {}

    # Build averaged symmetric distance matrix
    D = [[0.0]*n for _ in range(n)]
    for i, a in enumerate(node_ids):
        for j, b in enumerate(node_ids):
            if i == j:
                continue
            d1 = dist_matrix.get((a, b))
            d2 = dist_matrix.get((b, a))
            if d1 and d2:
                D[i][j] = (d1 + d2) / 2.0
            elif d1:
                D[i][j] = d1
            elif d2:
                D[i][j] = d2
            else:
                # Missing — estimate via shortest indirect path
                indirect = []
                for k, c in enumerate(node_ids):
                    if k == i or k == j:
                        continue
                    dik = dist_matrix.get((a, c)) or dist_matrix.get((c, a))
                    dkj = dist_matrix.get((c, b)) or dist_matrix.get((b, c))
                    if dik and dkj:
                        indirect.append(dik + dkj)
                D[i][j] = min(indirect) if indirect else 1.0

    if n == 2:
        d = D[0][1]
        return {
            node_ids[0]: (0.0, 0.0, 0.0),
            node_ids[1]: (d,   0.0, 0.0),
        }

    # Squared distance matrix
    D2 = [[D[i][j]**2 for j in range(n)] for i in range(n)]

    # Double-centre: B = -0.5 * H * D2 * H  where H = I - (1/n)*ones
    row_mean  = [sum(D2[i]) / n for i in range(n)]
    col_mean  = [sum(D2[i][j] for i in range(n)) / n for j in range(n)]
    grand_mean = sum(row_mean) / n
    B = [[-0.5*(D2[i][j] - row_mean[i] - col_mean[j] + grand_mean)
          for j in range(n)] for i in range(n)]

    # Power iteration for top 2 eigenvectors
    def matvec(M, v):
        return [sum(M[i][j]*v[j] for j in range(n)) for i in range(n)]

    def normalise(v):
        s = math.sqrt(sum(x*x for x in v))
        return ([x/s for x in v], s) if s > 1e-10 else (v, 0.0)

    def dot(a, b):
        return sum(a[i]*b[i] for i in range(n))

    # First eigenvector/value
    v1 = [1.0/math.sqrt(n)] * n
    for _ in range(150):
        v1 = matvec(B, v1)
        v1, _ = normalise(v1)
    lam1 = dot(v1, matvec(B, v1))

    # Deflate and get second
    B2 = [[B[i][j] - lam1*v1[i]*v1[j] for j in range(n)] for i in range(n)]
    v2 = [(-1.0 if i % 2 == 0 else 1.0) for i in range(n)]
    v2, _ = normalise(v2)
    for _ in range(150):
        v2 = matvec(B2, v2)
        proj = dot(v2, v1)
        v2 = [v2[i] - proj*v1[i] for i in range(n)]
        v2, _ = normalise(v2)
    lam2 = dot(v2, matvec(B, v2))

    s1 = math.sqrt(max(lam1, 0.0))
    s2 = math.sqrt(max(lam2, 0.0))

    # Raw MDS coordinates
    coords = {nid: (v1[i]*s1, v2[i]*s2, 0.0)
              for i, nid in enumerate(node_ids)}

    # ── Align coordinate frame ─────────────────────────────────────────────
    # 1. Translate: Node 0 → origin
    ox, oy, _ = coords[node_ids[0]]
    coords = {nid: (x-ox, y-oy, 0.0) for nid, (x, y, z) in coords.items()}

    # 2. Rotate: Node 1 → positive X axis
    ref = node_ids[1]
    rx, ry, _ = coords[ref]
    angle = math.atan2(ry, rx)
    ca, sa = math.cos(-angle), math.sin(-angle)
    coords = {nid: (x*ca - y*sa, x*sa + y*ca, 0.0)
              for nid, (x, y, z) in coords.items()}

    # 3. Flip X if Node 1 ended up negative
    if coords[ref][0] < 0:
        coords = {nid: (-x, y, 0.0) for nid, (x, y, z) in coords.items()}

    # 4. Flip Y if most nodes are negative
    neg_y = sum(1 for x, y, z in coords.values() if y < 0)
    if neg_y > n // 2:
        coords = {nid: (x, -y, 0.0) for nid, (x, y, z) in coords.items()}

    print("[MDS] Solved {} nodes".format(n))
    for nid, (x, y, z) in sorted(coords.items()):
        print("[MDS]   Node {} → ({:.3f}, {:.3f})".format(nid, x, y))

    return coords


# ── Bootstrap solver (used before full matrix is available) ───────────────────

def _circle_intersect(p0, d0, p1, d1, prefer_positive_y=True):
    x1, y1 = p0[0], p0[1]; x2, y2 = p1[0], p1[1]
    dx, dy = x2-x1, y2-y1
    D = math.sqrt(dx*dx + dy*dy)
    if D < 1e-6: return None
    d0 = min(d0, D+d1-1e-6); d1 = min(d1, D+d0-1e-6)
    d0 = max(d0, abs(D-d1)+1e-6)
    a = (d0*d0 - d1*d1 + D*D) / (2*D)
    h2 = d0*d0 - a*a
    if h2 < 0: h2 = 0.0
    h = math.sqrt(h2)
    mx = x1 + a*dx/D; my = y1 + a*dy/D
    px1, py1 = mx + h*dy/D, my - h*dx/D
    px2, py2 = mx - h*dy/D, my + h*dx/D
    if prefer_positive_y:
        return (px2, py2) if py2 >= py1 else (px1, py1)
    return (px1, py1) if py1 >= py2 else (px2, py2)


def _trilaterate_2d(p1, d1, p2, d2, p3, d3):
    x1,y1=p1; x2,y2=p2; x3,y3=p3
    A=2*(x2-x1); B=2*(y2-y1)
    C=d1*d1-d2*d2-x1*x1+x2*x2-y1*y1+y2*y2
    D=2*(x3-x1); E=2*(y3-y1)
    F=d1*d1-d3*d3-x1*x1+x3*x3-y1*y1+y3*y3
    denom=A*E-B*D
    if abs(denom)<1e-6: return None
    return ((C*E-F*B)/denom, (A*F-D*C)/denom)


def solve_position(measurements, current_pos=None):
    """
    Bootstrap solver for initial localisation before full distance matrix.
    measurements: list of ((x,y,z), distance_metres)
    """
    measurements = sorted(measurements, key=lambda m: m[1])
    n = len(measurements)
    if n == 0: return None
    if n == 1:
        p, d = measurements[0]
        return (p[0]+d, p[1], 0.0)
    if n == 2:
        (p0,d0),(p1,d1) = measurements
        r = _circle_intersect((p0[0],p0[1]),d0,(p1[0],p1[1]),d1,True)
        if r is None: return (p0[0]+d0, p0[1], 0.0)
        if current_pos is not None:
            r2 = _circle_intersect((p0[0],p0[1]),d0,(p1[0],p1[1]),d1,False)
            if r2:
                def d2d(a, b): return math.sqrt((a[0]-b[0])**2+(a[1]-b[1])**2)
                if d2d(r2, current_pos[:2]) < d2d(r, current_pos[:2]):
                    r = r2
        return (r[0], r[1], 0.0)
    if n == 3:
        (p1,d1),(p2,d2),(p3,d3) = measurements[:3]
        r = _trilaterate_2d((p1[0],p1[1]),d1,(p2[0],p2[1]),d2,(p3[0],p3[1]),d3)
        if r is None: return solve_position(measurements[:2], current_pos)
        return (r[0], r[1], 0.0)
    # 4+ least squares
    (p0,d0) = measurements[0]; x0,y0,z0 = p0
    A_rows, b_rows = [], []
    for (pi,di) in measurements[1:]:
        xi,yi,zi = pi
        A_rows.append([2*(xi-x0), 2*(yi-y0), 2*(zi-z0)])
        b_rows.append(d0*d0-di*di-x0*x0+xi*xi-y0*y0+yi*yi-z0*z0+zi*zi)
    AtA=[[0.0]*3 for _ in range(3)]; Atb=[0.0]*3
    for row,b in zip(A_rows,b_rows):
        for i in range(3):
            Atb[i]+=row[i]*b
            for j in range(3): AtA[i][j]+=row[i]*row[j]
    def det3(m):
        return (m[0][0]*(m[1][1]*m[2][2]-m[1][2]*m[2][1])
               -m[0][1]*(m[1][0]*m[2][2]-m[1][2]*m[2][0])
               +m[0][2]*(m[1][0]*m[2][1]-m[1][1]*m[2][0]))
    def repl(m,ci,v):
        r=[row[:] for row in m]; [r.__setitem__(i, r[i][:]) or r[i].__setitem__(ci,v[i]) for i in range(3)]; return r
    D=det3(AtA)
    if abs(D)<1e-9: return solve_position(measurements[:3],current_pos)
    return (det3(repl(AtA,0,Atb))/D,
            det3(repl(AtA,1,Atb))/D,
            det3(repl(AtA,2,Atb))/D)
