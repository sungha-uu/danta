"""Static GitHub Pages dashboard generation."""

from danta.dashboard.builder import build_dashboard, load_dashboard_report
from danta.dashboard.models import DashboardReport

__all__ = ["DashboardReport", "build_dashboard", "load_dashboard_report"]
