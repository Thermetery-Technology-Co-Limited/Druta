/*
 * FreeType module list for the native UI (FT_CONFIG_MODULES_H, set in CMakeLists.txt).
 * The UI loads only TrueType faces from the Windows font folder, so it builds the
 * TrueType driver, its SFNT and glyph-name helpers, the auto-hinter and the
 * anti-aliasing renderer. Same format as include/freetype/config/ftmodule.h.
 */
FT_USE_MODULE( FT_Module_Class, autofit_module_class )
FT_USE_MODULE( FT_Driver_ClassRec, tt_driver_class )
FT_USE_MODULE( FT_Module_Class, psnames_module_class )
FT_USE_MODULE( FT_Module_Class, sfnt_module_class )
FT_USE_MODULE( FT_Renderer_Class, ft_smooth_renderer_class )
