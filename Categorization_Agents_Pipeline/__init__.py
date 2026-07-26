"""Instagram bio categorization — an agentic, multi-agent pipeline.

Reads {username, bio} from MinIO, works out what each person is into, and writes
categories and structured tags back to the MinIO table the panels already read.

    from Categorization_Agents_Pipeline import CategorizationPipeline

    run = CategorizationPipeline(workers=8).run()

Internal use only: nothing here touches the UI, and no external service is
called. The agents are deterministic — a lexicon, a gazetteer and pattern
matching — so a verdict is reproducible and a wrong tag is a line in
lexicon.py you can fix, not a prompt to re-tune.

See README.md in this folder for the agent-by-agent walkthrough.
"""
from .contracts import CleanBio, Profile, Signal, Verdict
from .pipeline import CategorizationPipeline

__all__ = ["CategorizationPipeline", "Profile", "CleanBio", "Signal", "Verdict"]
