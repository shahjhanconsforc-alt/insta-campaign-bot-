"""The eight agents, one responsibility each.

    DataLoaderAgent      MinIO -> validated Profile records
    BioCleaningAgent     Profile -> CleanBio (normalized, parts pulled out)
    InterestAgent        CleanBio -> Signals for what the person is into
    ClassifierAgent      Signals -> the category set
    EntityAgent          CleanBio -> Signals for named things (places, teams, ...)
    TaggerAgent          Signals -> structured tags (`loc:Hunza`, `sport:Cricket`)
    ConfidenceAgent      Signals -> a score per conclusion, and the cut
    WriterAgent          Verdict -> the MinIO table

They are deliberately import-light and side-effect-free apart from the first and
last: everything in between is a pure function of its input, which is what lets
the orchestrator run N of them at once without any coordination.
"""
from .loader import DataLoaderAgent
from .cleaner import BioCleaningAgent
from .interests import InterestAgent
from .classifier import ClassifierAgent
from .entities import EntityAgent
from .tagger import TaggerAgent
from .confidence import ConfidenceAgent
from .writer import WriterAgent

__all__ = [
    "DataLoaderAgent", "BioCleaningAgent", "InterestAgent", "ClassifierAgent",
    "EntityAgent", "TaggerAgent", "ConfidenceAgent", "WriterAgent",
]
