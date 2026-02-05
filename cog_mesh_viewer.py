
"""
COG Mesh Viewer
===============
Renders a 3D terrain mesh from Cloud-Optimized GeoTIFFs:
  1) DEM COG -> mesh vertex elevations
  2) RGB COG -> texture draped over mesh

Uses moderngl + glfw for OpenGL rendering, osgeo.gdal for COG access.

Usage:
  python cog_mesh_viewer.py
  python cog_mesh_viewer.py --dem URL --rgb URL --bbox xmin,ymin,xmax,ymax --crs EPSG:XXXX
"""

import argparse
import sys
import math
import numpy as np
import glfw
import moderngl
from pyrr import Matrix44, Vector3
from osgeo import gdal, osr

gdal.UseExceptions()

# ─── Configuration defaults ──────────────────────────────────────────────────

# Hobart area default bbox (EPSG:4326)
DEFAULT_BBOX = [147.2, -43.0, 147.5, -42.75]
DEFAULT_CRS = "EPSG:4326"

# DEM: auto-constructed from bbox using Copernicus 30m COGs on AWS
# Override with --dem to use any GDAL-readable source
DEFAULT_DEM = "auto"

# RGB: Esri World Imagery via WMTS
DEFAULT_RGB = "WMTS:https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/WMTS/1.0.0/WMTSCapabilities.xml,layer=World_Imagery"

# Mesh resolution
MESH_NX = 128
MESH_NY = 128

# ─── GDAL helpers ────────────────────────────────────────────────────────────

def copernicus_dem_tiles(bbox_4326):
    """
    Construct /vsicurl/ paths for Copernicus 30m DEM COG tiles covering a bbox.
    bbox_4326: [xmin, ymin, xmax, ymax] in EPSG:4326.
    Returns list of paths (one per 1° tile).
    """
    import math
    base = "https://copernicus-dem-30m.s3.amazonaws.com"
    
    lon_min = int(math.floor(bbox_4326[0]))
    lon_max = int(math.floor(bbox_4326[2]))
    lat_min = int(math.floor(bbox_4326[1]))
    lat_max = int(math.floor(bbox_4326[3]))
    
    paths = []
    for lat in range(lat_min, lat_max + 1):
        for lon in range(lon_min, lon_max + 1):
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            lat_str = f"{abs(lat):02d}"
            lon_str = f"{abs(lon):03d}"
            tile = f"Copernicus_DSM_COG_10_{ns}{lat_str}_00_{ew}{lon_str}_00_DEM"
            paths.append(f"/vsicurl/{base}/{tile}/{tile}.tif")
    
    return paths


def build_dem_vrt(bbox_4326):
    """Build a VRT mosaic of Copernicus DEM tiles covering bbox. Returns path."""
    tiles = copernicus_dem_tiles(bbox_4326)
    if len(tiles) == 1:
        return tiles[0]  # No VRT needed
    vrt_path = "/vsimem/cop_dem.vrt"
    print(f"  Mosaicking {len(tiles)} DEM tiles via VRT")
    gdal.BuildVRT(vrt_path, tiles)
    return vrt_path

def get_cog_crs(path):
    """Get the CRS of a GDAL source as an osr.SpatialReference."""
    ds = gdal.Open(path)
    if ds is None:
        raise RuntimeError(f"Cannot open: {path}")
    sr = osr.SpatialReference()
    sr.ImportFromWkt(ds.GetProjectionRef())
    ds = None
    return sr


def transform_bbox(bbox, src_crs_str, dst_crs_str):
    """Transform [xmin, ymin, xmax, ymax] between CRS strings."""
    src_sr = osr.SpatialReference()
    src_sr.SetFromUserInput(src_crs_str)
    src_sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst_sr = osr.SpatialReference()
    dst_sr.SetFromUserInput(dst_crs_str)
    dst_sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src_sr, dst_sr)
    ll = tr.TransformPoint(bbox[0], bbox[1])
    ur = tr.TransformPoint(bbox[2], bbox[3])
    return [ll[0], ll[1], ur[0], ur[1]]


def read_cog_bbox(path, bbox_native, width, height, bands=None):
    """
    Read a GDAL source within bbox_native (in the source's own CRS) at target resolution.
    GDAL picks the best overview automatically.
    
    Returns numpy array: (height, width) for single band, (bands, height, width) for multi.
    """
    ds = gdal.Open(path)
    if ds is None:
        raise RuntimeError(f"Cannot open: {path}")
    
    gt = ds.GetGeoTransform()
    
    # Convert bbox to pixel coordinates
    px0 = (bbox_native[0] - gt[0]) / gt[1]
    py0 = (bbox_native[3] - gt[3]) / gt[5]
    px1 = (bbox_native[2] - gt[0]) / gt[1]
    py1 = (bbox_native[1] - gt[3]) / gt[5]
    
    # Clamp to raster extent
    px0 = max(0, int(math.floor(px0)))
    py0 = max(0, int(math.floor(py0)))
    px1 = min(ds.RasterXSize, int(math.ceil(px1)))
    py1 = min(ds.RasterYSize, int(math.ceil(py1)))
    
    src_w = px1 - px0
    src_h = py1 - py0
    
    if src_w <= 0 or src_h <= 0:
        raise RuntimeError(f"Bbox doesn't overlap raster. Pixel window: ({px0},{py0})-({px1},{py1}), raster: {ds.RasterXSize}x{ds.RasterYSize}")
    
    if bands is None:
        bands = ds.RasterCount
    
    if bands == 1:
        data = ds.GetRasterBand(1).ReadAsArray(
            px0, py0, src_w, src_h,
            buf_xsize=width, buf_ysize=height
        )
    else:
        data = ds.ReadAsArray(
            px0, py0, src_w, src_h,
            buf_xsize=width, buf_ysize=height,
            band_list=list(range(1, bands + 1))
        )
    
    nodata = ds.GetRasterBand(1).GetNoDataValue()
    ds = None
    
    if nodata is not None:
        data = np.where(data == nodata, 0, data)
    
    return data.astype(np.float32) if bands == 1 else data


def bbox_aspect_ratio(bbox, crs_str):
    """
    Compute X/Y aspect ratio for a bbox, correcting for latitude if geographic CRS.
    Returns (x_scale, y_scale) normalized so the larger is 1.0.
    """
    dx = bbox[2] - bbox[0]
    dy = bbox[3] - bbox[1]
    
    # If geographic, correct for latitude compression
    sr = osr.SpatialReference()
    sr.SetFromUserInput(crs_str)
    if sr.IsGeographic():
        mid_lat = math.radians((bbox[1] + bbox[3]) / 2.0)
        dx *= math.cos(mid_lat)
    
    if dx >= dy:
        return (1.0, dy / dx)
    else:
        return (dx / dy, 1.0)


# ─── Mesh generation ────────────────────────────────────────────────────────

def make_mesh(nx, ny, elevations, z_scale=1.0, z_offset=0.0, xy_scale=(1.0, 1.0)):
    """
    Build a mesh with correct aspect ratio.
    
    xy_scale: (x_scale, y_scale) from bbox_aspect_ratio().
    Positions use scaled coords, texcoords stay in [0,1]^2.
    
    Returns (vertices, indices):
      vertices: float32 array, interleaved (x, y, z, u, v) per vertex
      indices: uint32 array of triangle indices
    """
    u = np.linspace(0, 1, nx, dtype=np.float32)
    v = np.linspace(0, 1, ny, dtype=np.float32)
    uu, vv = np.meshgrid(u, v)  # (ny, nx) each
    
    zz = (elevations.astype(np.float32) - z_offset) * z_scale
    
    vertices = np.zeros((ny, nx, 5), dtype=np.float32)
    vertices[:, :, 0] = uu * xy_scale[0]   # x position (aspect-corrected)
    vertices[:, :, 1] = vv * xy_scale[1]   # y position (aspect-corrected)
    vertices[:, :, 2] = zz
    vertices[:, :, 3] = uu                  # tex_u (always 0-1)
    vertices[:, :, 4] = vv                   # tex_v (always 0-1, no flip needed)
    
    # Triangle strip indices
    indices = []
    for j in range(ny - 1):
        for i in range(nx - 1):
            idx = j * nx + i
            # Two triangles per quad
            indices.extend([idx, idx + 1, idx + nx])
            indices.extend([idx + 1, idx + nx + 1, idx + nx])
    
    return vertices.reshape(-1), np.array(indices, dtype=np.uint32)


# ─── Shaders ─────────────────────────────────────────────────────────────────

VERTEX_SHADER = '''
#version 330
uniform mat4 mvp;

in vec3 in_position;
in vec2 in_texcoord;

out vec2 v_texcoord;
out float v_elevation;

void main() {
    gl_Position = mvp * vec4(in_position, 1.0);
    v_texcoord = in_texcoord;
    v_elevation = in_position.z;
}
'''

FRAGMENT_SHADER = '''
#version 330
uniform sampler2D tex;
uniform bool use_texture;
uniform float elev_min;
uniform float elev_max;

in vec2 v_texcoord;
in float v_elevation;

out vec4 fragColor;

void main() {
    if (use_texture) {
        fragColor = texture(tex, v_texcoord);
    } else {
        // Terrain colormap: blue -> green -> brown -> white
        float t = clamp((v_elevation - elev_min) / (elev_max - elev_min + 0.001), 0.0, 1.0);
        vec3 color;
        if (t < 0.25) {
            color = mix(vec3(0.1, 0.3, 0.6), vec3(0.2, 0.6, 0.3), t / 0.25);
        } else if (t < 0.5) {
            color = mix(vec3(0.2, 0.6, 0.3), vec3(0.5, 0.7, 0.2), (t - 0.25) / 0.25);
        } else if (t < 0.75) {
            color = mix(vec3(0.5, 0.7, 0.2), vec3(0.6, 0.4, 0.2), (t - 0.5) / 0.25);
        } else {
            color = mix(vec3(0.6, 0.4, 0.2), vec3(0.95, 0.95, 0.95), (t - 0.75) / 0.25);
        }
        fragColor = vec4(color, 1.0);
    }
}
'''

# ─── Camera ──────────────────────────────────────────────────────────────────

class OrbitCamera:
    """Simple orbit camera around a target point."""
    
    def __init__(self, target=(0.5, 0.5, 0.0), distance=1.5, azimuth=45.0, elevation=35.0):
        self.target = list(target)
        self.distance = distance
        self.azimuth = azimuth    # degrees, horizontal rotation
        self.elevation = elevation  # degrees, vertical angle
        self.aspect = 1.0
        
        # Mouse state
        self._dragging = False
        self._panning = False
        self._last_x = 0
        self._last_y = 0
    
    def get_view_matrix(self):
        az = math.radians(self.azimuth)
        el = math.radians(self.elevation)
        
        # Spherical to cartesian offset from target
        dx = self.distance * math.cos(el) * math.cos(az)
        dy = self.distance * math.cos(el) * math.sin(az)
        dz = self.distance * math.sin(el)
        
        eye = [self.target[0] + dx, self.target[1] + dy, self.target[2] + dz]
        
        return Matrix44.look_at(
            eye,
            self.target,
            [0.0, 0.0, 1.0]  # Z-up
        )
    
    def get_projection_matrix(self):
        return Matrix44.perspective_projection(50.0, self.aspect, 0.001, 100.0)
    
    def get_mvp(self):
        return self.get_projection_matrix() * self.get_view_matrix()
    
    def on_mouse_button(self, window, button, action, mods):
        if button == glfw.MOUSE_BUTTON_LEFT:
            self._dragging = (action == glfw.PRESS)
            if self._dragging:
                self._last_x, self._last_y = glfw.get_cursor_pos(window)
        elif button == glfw.MOUSE_BUTTON_RIGHT:
            self._panning = (action == glfw.PRESS)
            if self._panning:
                self._last_x, self._last_y = glfw.get_cursor_pos(window)
    
    def on_cursor_pos(self, window, x, y):
        dx = x - self._last_x
        dy = y - self._last_y
        self._last_x = x
        self._last_y = y
        
        if self._dragging:
            self.azimuth -= dx * 0.3
            self.elevation += dy * 0.3
            self.elevation = max(-89, min(89, self.elevation))
        
        if self._panning:
            # Pan in the view plane
            speed = self.distance * 0.002
            az = math.radians(self.azimuth)
            # Right vector (perpendicular to view direction in XY plane)
            rx, ry = -math.sin(az), math.cos(az)
            self.target[0] += rx * dx * speed
            self.target[1] += ry * dx * speed
            self.target[2] += dy * speed
    
    def on_scroll(self, window, xoffset, yoffset):
        self.distance *= 0.9 if yoffset > 0 else 1.1
        self.distance = max(0.01, min(50.0, self.distance))


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="COG Mesh Viewer")
    parser.add_argument("--dem", default=DEFAULT_DEM, help="DEM COG URL or /vsicurl/ path")
    parser.add_argument("--rgb", default=DEFAULT_RGB, help="RGB COG URL or /vsicurl/ path")
    parser.add_argument("--bbox", default=None, help="xmin,ymin,xmax,ymax")
    parser.add_argument("--crs", default=DEFAULT_CRS, help="CRS of the mesh (default EPSG:4326)")
    parser.add_argument("--bbox-crs", default=None, help="CRS of the bbox (default: same as --crs, typically EPSG:4326 for convenience)")
    parser.add_argument("--nx", type=int, default=MESH_NX, help="Mesh X resolution")
    parser.add_argument("--ny", type=int, default=MESH_NY, help="Mesh Y resolution")
    parser.add_argument("--zscale", type=float, default=None, help="Z exaggeration (auto if not set)")
    args = parser.parse_args()
    
    bbox_input = [float(x) for x in args.bbox.split(",")] if args.bbox else DEFAULT_BBOX
    bbox_crs = args.bbox_crs or args.crs
    bbox = bbox_input
    
    # Transform bbox into mesh CRS if they differ
    if bbox_crs.upper() != args.crs.upper():
        bbox = transform_bbox(bbox_input, bbox_crs, args.crs)
        print(f"Bbox (input):  {bbox_input} ({bbox_crs})")
        print(f"Bbox (mesh):   {bbox} ({args.crs})")
    else:
        print(f"Bbox: {bbox} ({args.crs})")
    
    print(f"Mesh: {args.nx} x {args.ny}")
    
    # ── Resolve DEM source ──
    dem_path = args.dem
    if dem_path == "auto":
        # Need bbox in 4326 for tile name lookup
        if args.crs.upper() != "EPSG:4326":
            bbox_4326 = transform_bbox(bbox, args.crs, "EPSG:4326")
        else:
            bbox_4326 = bbox
        dem_path = build_dem_vrt(bbox_4326)
        print(f"  Auto DEM: {dem_path}")
    
    # ── Read DEM in its native CRS ──
    print(f"Reading DEM: {dem_path}")
    dem_crs = get_cog_crs(dem_path)
    dem_crs_str = dem_crs.ExportToWkt()
    if args.crs.upper() != dem_crs_str:
        dem_bbox = transform_bbox(bbox, args.crs, dem_crs_str)
    else:
        dem_bbox = bbox
    print(f"  Bbox in DEM CRS: {[f'{x:.2f}' for x in dem_bbox]}")
    
    dem_data = read_cog_bbox(dem_path, dem_bbox, args.nx, args.ny, bands=1)
    print(f"  Elevation range: {dem_data.min():.1f} to {dem_data.max():.1f}")
    
    # ── Read RGB texture in its native CRS (if provided) ──
    rgb_data = None
    has_texture = False
    if args.rgb and args.rgb.lower() != "none":
        rgb_path = args.rgb
        
        tex_w = args.nx * 4
        tex_h = args.ny * 4
        print(f"Reading RGB: {rgb_path}")
        
        rgb_crs = get_cog_crs(rgb_path)
        rgb_crs_str = rgb_crs.ExportToWkt()
        if args.crs.upper() != rgb_crs_str:
            rgb_bbox = transform_bbox(bbox, args.crs, rgb_crs_str)
        else:
            rgb_bbox = bbox
        
        rgb_data = read_cog_bbox(rgb_path, rgb_bbox, tex_w, tex_h, bands=3)
        
        # (bands, h, w) -> (h, w, bands), uint8
        if rgb_data.ndim == 3:
            rgb_data = np.moveaxis(rgb_data, 0, -1)
        if rgb_data.dtype != np.uint8:
            rmin, rmax = rgb_data.min(), rgb_data.max()
            if rmax > 255:
                rgb_data = ((rgb_data - rmin) / (rmax - rmin) * 255).astype(np.uint8)
            else:
                rgb_data = rgb_data.astype(np.uint8)
        rgb_data = np.ascontiguousarray(rgb_data)
        has_texture = True
        print(f"  Texture: {tex_w} x {tex_h}")
    
    # ── Build mesh ──
    elev_min = float(dem_data.min())
    elev_max = float(dem_data.max())
    elev_range = elev_max - elev_min
    
    # Auto z-scale: map elevation range to ~0.3 units relative to XY unit square
    if args.zscale is not None:
        z_scale = args.zscale
    else:
        z_scale = 0.3 / max(elev_range, 1.0)
    
    print(f"  Z scale: {z_scale:.6f} (range {elev_range:.1f}m -> {elev_range * z_scale:.3f} units)")
    
    # ── Aspect ratio ──
    xy_scale = bbox_aspect_ratio(bbox, args.crs)
    print(f"  Aspect ratio: x={xy_scale[0]:.3f}, y={xy_scale[1]:.3f}")
    
    vertices, indices = make_mesh(args.nx, args.ny, dem_data, z_scale=z_scale, z_offset=elev_min, xy_scale=xy_scale)
    
    # ── OpenGL setup ──
    if not glfw.init():
        print("Failed to init GLFW")
        sys.exit(1)
    
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)
    
    window = glfw.create_window(1280, 900, "COG Mesh Viewer", None, None)
    if not window:
        glfw.terminate()
        print("Failed to create window")
        sys.exit(1)
    
    glfw.make_context_current(window)
    ctx = moderngl.create_context()
    ctx.enable(moderngl.DEPTH_TEST)
    
    # ── Shader program ──
    prog = ctx.program(vertex_shader=VERTEX_SHADER, fragment_shader=FRAGMENT_SHADER)
    
    # ── Buffers ──
    vbo = ctx.buffer(vertices.tobytes())
    ibo = ctx.buffer(indices.tobytes())
    vao = ctx.vertex_array(prog, [(vbo, '3f 2f', 'in_position', 'in_texcoord')], ibo)
    
    # ── Texture ──
    if has_texture:
        h, w, _ = rgb_data.shape
        texture = ctx.texture((w, h), 3, rgb_data.tobytes())
        texture.filter = (moderngl.LINEAR_MIPMAP_LINEAR, moderngl.LINEAR)
        texture.build_mipmaps()
    
    # ── Camera ──
    z_mid = (elev_max - elev_min) * z_scale * 0.5
    camera = OrbitCamera(
        target=(xy_scale[0] / 2, xy_scale[1] / 2, z_mid),
        distance=max(xy_scale[0], xy_scale[1]) * 1.5
    )
    
    # ── Wire up input ──
    glfw.set_mouse_button_callback(window, camera.on_mouse_button)
    glfw.set_cursor_pos_callback(window, camera.on_cursor_pos)
    glfw.set_scroll_callback(window, camera.on_scroll)
    
    # Wireframe toggle and dynamic z
    wireframe = False
    current_z_scale = z_scale
    
    def rebuild_mesh(new_z_scale):
        """Rebuild vertex buffer with new z exaggeration."""
        nonlocal current_z_scale, vbo, vao
        current_z_scale = new_z_scale
        new_verts, _ = make_mesh(args.nx, args.ny, dem_data, z_scale=current_z_scale, z_offset=elev_min, xy_scale=xy_scale)
        vbo.write(new_verts.tobytes())
        prog['elev_max'].value = elev_range * current_z_scale
        # Update camera target z
        camera.target[2] = (elev_max - elev_min) * current_z_scale * 0.5
    
    def on_key(window, key, scancode, action, mods):
        nonlocal wireframe, current_z_scale
        if action == glfw.PRESS or action == glfw.REPEAT:
            if key == glfw.KEY_ESCAPE:
                glfw.set_window_should_close(window, True)
            elif key == glfw.KEY_W and action == glfw.PRESS:
                wireframe = not wireframe
            elif key == glfw.KEY_T and action == glfw.PRESS:
                if has_texture:
                    prog['use_texture'].value = not prog['use_texture'].value
            elif key == glfw.KEY_UP:
                rebuild_mesh(current_z_scale * 1.2)
                print(f"  Z exaggeration: {current_z_scale / z_scale:.1f}x")
            elif key == glfw.KEY_DOWN:
                rebuild_mesh(current_z_scale / 1.2)
                print(f"  Z exaggeration: {current_z_scale / z_scale:.1f}x")
    
    glfw.set_key_callback(window, on_key)
    
    # ── Set uniforms ──
    prog['use_texture'].value = has_texture
    prog['elev_min'].value = 0.0  # after offset, min is 0
    prog['elev_max'].value = elev_range * z_scale
    if has_texture:
        prog['tex'].value = 0
    
    print("\n── Controls ──")
    print("  Left-drag:   orbit")
    print("  Right-drag:  pan")
    print("  Scroll:      zoom")
    print("  W:           toggle wireframe")
    print("  Up/Down:     z exaggeration")
    if has_texture:
        print("  T:           toggle texture / elevation colormap")
    print("  Esc:         quit")
    print()
    
    # ── Render loop ──
    while not glfw.window_should_close(window):
        glfw.poll_events()
        
        w, h = glfw.get_framebuffer_size(window)
        ctx.viewport = (0, 0, w, h)
        camera.aspect = w / max(h, 1)
        
        ctx.clear(0.08, 0.08, 0.12)
        
        if wireframe:
            ctx.wireframe = True
        
        mvp = camera.get_mvp()
        prog['mvp'].write(mvp.astype('f4').tobytes())
        
        if has_texture:
            texture.use(0)
        
        vao.render()
        
        ctx.wireframe = False
        
        glfw.swap_buffers(window)
    
    glfw.terminate()
    print("Done.")


if __name__ == "__main__":
    main()
