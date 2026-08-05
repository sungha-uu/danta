"""Active environment-neutral autonomous campaign API.

The implementation retains compatibility with archived paper test fixtures in
the legacy module, while production code imports only this public surface.
"""

from danta.services.paper_autonomous_campaign import (
    AutonomousCampaignAuthorization,
    AutonomousCampaignController,
    AutonomousCampaignState,
    create_campaign_authorization,
    load_campaign_authorization,
    write_campaign_authorization,
)

__all__ = [
    "AutonomousCampaignAuthorization",
    "AutonomousCampaignController",
    "AutonomousCampaignState",
    "create_campaign_authorization",
    "load_campaign_authorization",
    "write_campaign_authorization",
]
