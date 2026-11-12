"""真实浏览器指纹常量（照抄参考实现，别自己造）。

`REAL_HIST` 是 256 桶 canvas 直方图，和固定为 36000；`GPU_EXT` 是 macOS
Chrome 131 的 WebGL 扩展表。自己随机生成的直方图/空扩展表是明显的机器人
特征（AWS 风控会直接 BLOCKED）。
"""

REAL_HIST = (
    13847, 42, 42, 40, 62, 33, 47, 29, 41, 37, 32, 25,
    27, 53, 23, 22, 31, 20, 24, 30, 34, 20, 37, 15,
    21, 32, 25, 26, 66, 25, 16, 27, 26, 19, 22, 32,
    38, 15, 39, 35, 49, 9, 29, 43, 16, 26, 23, 15,
    29, 30, 36, 46, 18, 29, 11, 30, 24, 27, 20, 27,
    22, 19, 20, 38, 171, 32, 25, 33, 15, 15, 6, 22,
    11, 39, 31, 24, 18, 12, 17, 34, 17, 30, 17, 27,
    25, 28, 20, 19, 19, 19, 24, 34, 10, 24, 14, 27,
    28, 18, 31, 27, 78, 28, 512, 41, 28, 22, 34, 15,
    26, 29, 34, 16, 17, 21, 17, 43, 17, 9, 24, 34,
    17, 14, 26, 6, 20, 39, 28, 25, 230, 20, 44, 19,
    8, 24, 18, 12, 28, 16, 5, 28, 37, 21, 11, 27,
    22, 30, 18, 16, 25, 18, 11, 25, 30, 104, 13, 38,
    22, 22, 42, 18, 23, 22, 32, 9, 30, 18, 5, 31,
    34, 18, 24, 17, 22, 25, 12, 16, 34, 18, 28, 23,
    15, 55, 45, 18, 21, 31, 21, 28, 22, 21, 31, 30,
    145, 29, 19, 34, 18, 21, 24, 37, 30, 19, 49, 34,
    62, 62, 23, 24, 21, 40, 27, 30, 37, 22, 38, 51,
    40, 37, 29, 27, 53, 28, 31, 27, 37, 36, 40, 57,
    31, 22, 41, 32, 23, 35, 28, 58, 41, 45, 27, 38,
    36, 48, 49, 30, 37, 78, 56, 36, 40, 62, 48, 81,
    70, 59, 94, 13740,
)


GPU_EXT = (
    'ANGLE_instanced_arrays', 'EXT_blend_minmax', 'EXT_clip_control',
    'EXT_color_buffer_half_float', 'EXT_depth_clamp', 'EXT_disjoint_timer_query',
    'EXT_float_blend', 'EXT_frag_depth', 'EXT_polygon_offset_clamp',
    'EXT_shader_texture_lod', 'EXT_texture_compression_bptc', 'EXT_texture_compression_rgtc',
    'EXT_texture_filter_anisotropic', 'EXT_texture_mirror_clamp_to_edge', 'EXT_sRGB',
    'KHR_parallel_shader_compile', 'OES_element_index_uint', 'OES_fbo_render_mipmap',
    'OES_standard_derivatives', 'OES_texture_float', 'OES_texture_float_linear',
    'OES_texture_half_float', 'OES_texture_half_float_linear', 'OES_vertex_array_object',
    'WEBGL_blend_func_extended', 'WEBGL_color_buffer_float', 'WEBGL_compressed_texture_astc',
    'WEBGL_compressed_texture_etc', 'WEBGL_compressed_texture_etc1', 'WEBGL_compressed_texture_pvrtc',
    'WEBGL_compressed_texture_s3tc', 'WEBGL_compressed_texture_s3tc_srgb', 'WEBGL_debug_renderer_info',
    'WEBGL_debug_shaders', 'WEBGL_depth_texture', 'WEBGL_draw_buffers',
    'WEBGL_lose_context', 'WEBGL_multi_draw', 'WEBGL_polygon_mode',
)
