"""Deterministic and vision-assisted editable PowerPoint conversion pipelines."""

from .figure import FigureConversionOptions, convert_figure, figure_to_slide_spec, load_figure
from .figure_optimizer import run_target_optimizer
from .figure_refinement import FigureOptimizationAdvice, FigureReview
from .models import SlideSpec
from .pptx_shape_replace import replace_shape_geometry
from .version import __version__

__all__ = [
    "FigureConversionOptions",
    "FigureReview",
    "FigureOptimizationAdvice",
    "SlideSpec",
    "convert_figure",
    "figure_to_slide_spec",
    "load_figure",
    "replace_shape_geometry",
    "run_target_optimizer",
]
