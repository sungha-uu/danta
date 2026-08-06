"""Active environment-neutral autonomous campaign API.

The implementation retains compatibility with archived paper test fixtures in
the legacy module, while production code imports only this public surface.
"""

from danta.services.paper_autonomous_campaign import (
    AutonomousCampaignAuthorization,
    AutonomousCampaignController,
    AutonomousCampaignState,
    AutonomousCandidatePreference,
    candidate_preference_path,
    create_campaign_authorization,
    load_campaign_authorization,
    load_candidate_preference,
    read_candidate_preference,
    write_campaign_authorization,
    write_candidate_preference,
)

__all__ = [
    "AutonomousCampaignAuthorization",
    "AutonomousCandidatePreference",
    "AutonomousCampaignController",
    "AutonomousCampaignState",
    "candidate_preference_path",
    "create_campaign_authorization",
    "load_candidate_preference",
    "load_campaign_authorization",
    "read_candidate_preference",
    "write_candidate_preference",
    "write_campaign_authorization",
]
