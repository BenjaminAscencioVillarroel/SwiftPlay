"""Genera el ícono plateado mate transparente assets/swiftplay.ico y assets/swiftplay.png."""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = ROOT_DIR / "assets"

ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]


def _superellipse_points(
    cx: float, cy: float, a: float, b: float, n: float = 5.4, steps: int = 360
) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    exp = 2.0 / n
    for i in range(steps):
        theta = (2.0 * math.pi * i) / steps
        ct = math.cos(theta)
        st = math.sin(theta)
        x = cx + a * math.copysign(abs(ct) ** exp, ct)
        y = cy + b * math.copysign(abs(st) ** exp, st)
        pts.append((x, y))
    return pts


def _rounded_triangle_mask(
    size: int, vertices: list[tuple[float, float]], corner_radius: float
) -> Image.Image:
    """Genera una máscara anti-aliased de triángulo con vértices suavemente redondeados."""
    # Trazamos un triángulo ligeramente encogido y lo dilatamos con un borde redondeado
    # equivalente usando círculos en vértices + barras en los lados para precisión exacta.
    cx = sum(x for x, _ in vertices) / 3.0
    cy = sum(y for _, y in vertices) / 3.0

    # Escalamos hacia adentro para compensar el radio de esquina
    inner: list[tuple[float, float]] = []
    # Calculamos distancia promedio del centro a los vértices para encoger geométricamente
    for x, y in vertices:
        dx, dy = x - cx, y - cy
        dist = math.hypot(dx, dy)
        scale = max(0.2, (dist - corner_radius * 1.35) / dist)
        inner.append((cx + dx * scale, cy + dy * scale))

    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.polygon(inner, fill=255)
    w = max(2, int(round(corner_radius * 2)))
    for i in range(3):
        p_a = inner[i]
        p_b = inner[(i + 1) % 3]
        d.line([p_a, p_b], fill=255, width=w)
    for px, py in inner:
        d.ellipse(
            [px - corner_radius, py - corner_radius, px + corner_radius, py + corner_radius],
            fill=255,
        )
    return mask


def _diagonal_gradient_rgb(
    size: int, top_left: tuple[int, int, int], mid: tuple[int, int, int], bottom_right: tuple[int, int, int]
) -> Image.Image:
    # Construimos un gradiente diagonal suave 256x256 y escalamos bicúbico para velocidad y suavidad
    g_size = 256
    grad = Image.new("RGB", (g_size, g_size))
    px = grad.load()
    denom = float((g_size - 1) * 2)
    for y in range(g_size):
        for x in range(g_size):
            t = (x * 0.85 + y * 1.15) / denom
            t = max(0.0, min(1.0, t))
            if t < 0.5:
                u = t * 2.0
                r = int(top_left[0] * (1 - u) + mid[0] * u)
                g = int(top_left[1] * (1 - u) + mid[1] * u)
                b = int(top_left[2] * (1 - u) + mid[2] * u)
            else:
                u = (t - 0.5) * 2.0
                r = int(mid[0] * (1 - u) + bottom_right[0] * u)
                g = int(mid[1] * (1 - u) + bottom_right[1] * u)
                b = int(mid[2] * (1 - u) + bottom_right[2] * u)
            px[x, y] = (r, g, b)
    return grad.resize((size, size), Image.Resampling.BICUBIC)


def _create_base_icon(size: int = 1024) -> Image.Image:
    cx = size * 0.5
    cy = size * 0.5
    a = size * 0.455
    b = size * 0.320
    n = 5.4

    # 1) Silueta exterior del botón YouTube (sin caja cuadrada)
    outer_pts = _superellipse_points(cx, cy, a, b, n=n, steps=360)
    outer_mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(outer_mask).polygon(outer_pts, fill=255)

    # Cara interior (dejando bisel metálico perimetral)
    bevel_w = size * 0.028
    inner_pts = _superellipse_points(cx, cy, a - bevel_w, b - bevel_w, n=n, steps=360)
    inner_mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(inner_mask).polygon(inner_pts, fill=255)
    inner_soft = inner_mask.filter(ImageFilter.GaussianBlur(radius=size * 0.008))

    # 2) Superficie plateado mate (anodizado satinado)
    face_rgb = _diagonal_gradient_rgb(
        size,
        top_left=(227, 231, 238),     # #E3E7EE
        mid=(196, 202, 211),          # #C4CAD3
        bottom_right=(154, 162, 175), # #9AA2AF
    )

    # Brillo satinado muy sutil de aluminio cepillado horizontal
    brushed = Image.new("RGB", (1, size))
    b_px = brushed.load()
    for y in range(size):
        wave = math.sin(y * 0.37) * 2.0 + math.cos(y * 0.91) * 1.2
        val = int(max(0, min(255, 128 + wave)))
        b_px[0, y] = (val, val, val)
    brushed_full = brushed.resize((size, size), Image.Resampling.BILINEAR)
    face_rgb = ImageChops.soft_light(face_rgb, brushed_full)

    # Bisel perimetral mecanizado (luz arriba-izquierda, sombra abajo-derecha)
    rim_rgb = _diagonal_gradient_rgb(
        size,
        top_left=(246, 248, 251),     # #F6F8FB
        mid=(180, 187, 198),          # #B4BBC6
        bottom_right=(110, 117, 130), # #6E7582
    )
    surface_rgb = Image.composite(face_rgb, rim_rgb, inner_soft)

    # Borde fino exterior para definición limpia en fondos claros y oscuros
    outer_edge = ImageChops.subtract(
        outer_mask,
        outer_mask.filter(ImageFilter.MinFilter(size=9)),
    )
    edge_rgb = Image.new("RGB", (size, size), (94, 101, 114))
    surface_rgb = Image.composite(edge_rgb, surface_rgb, outer_edge)

    # 3) Triángulo central recortado (transparente 100%) con bisel interno hundido
    tri_h = size * 0.310
    tri_w = tri_h * 0.88
    tri_cx = cx + size * 0.016
    tri_cy = cy
    tri_verts = [
        (tri_cx - tri_w * 0.44, tri_cy - tri_h * 0.50),
        (tri_cx - tri_w * 0.44, tri_cy + tri_h * 0.50),
        (tri_cx + tri_w * 0.56, tri_cy),
    ]
    cutout_mask = _rounded_triangle_mask(size, tri_verts, corner_radius=size * 0.016)

    # Anillo biselado alrededor del recorte del triángulo (pared top-izq sombreada, inf-der iluminada)
    outer_cut_ring = _rounded_triangle_mask(size, [
        (tri_verts[0][0] - size * 0.012, tri_verts[0][1] - size * 0.014),
        (tri_verts[1][0] - size * 0.012, tri_verts[1][1] + size * 0.014),
        (tri_verts[2][0] + size * 0.016, tri_verts[2][1]),
    ], corner_radius=size * 0.020)
    cut_ring_mask = ImageChops.subtract(outer_cut_ring, cutout_mask)
    cut_ring_soft = cut_ring_mask.filter(ImageFilter.GaussianBlur(radius=size * 0.003))

    recessed_bevel_rgb = _diagonal_gradient_rgb(
        size,
        top_left=(112, 120, 133),     # #707885 sombra en pared interna superior-izquierda
        mid=(176, 183, 195),
        bottom_right=(237, 240, 245), # #EDF0F5 luz en borde inferior-derecho
    )
    surface_rgb = Image.composite(recessed_bevel_rgb, surface_rgb, cut_ring_soft)

    # 4) Canal Alfa final: botón sólido menos el recorte del triángulo central
    final_alpha = ImageChops.subtract(outer_mask, cutout_mask)

    rgba = surface_rgb.convert("RGBA")
    rgba.putalpha(final_alpha)
    return rgba


def main() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    base = _create_base_icon(1024)

    png_path = ASSETS_DIR / "swiftplay.png"
    png_256 = base.resize((256, 256), Image.Resampling.LANCZOS)
    png_256.save(png_path, format="PNG")

    ico_path = ASSETS_DIR / "swiftplay.ico"
    icons = [base.resize((s, s), Image.Resampling.LANCZOS) for s in ICON_SIZES]
    icons[-1].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in ICON_SIZES],
        append_images=icons[:-1],
    )
    print(f"Generado: {png_path} y {ico_path}")


if __name__ == "__main__":
    main()
