commit 4c53a9d86b4236b1e27c5fb58a963aa3619f6810
Author: Treystu <Treystu@users.noreply.github.com>
Date:   Sun Sep 20 20:36:43 2026 -1000

    fix(jev-completion): union jev-phase + mission CLI after origin/main merge

diff --git a/harness/cli.py b/harness/cli.py
index b682aa0..93c3149 100644
--- a/harness/cli.py
+++ b/harness/cli.py
@@ -42,11 +42,8 @@ from .service import read_text_file as _service_read_text
 from .rankings import build_rankings_report as _rankings_report
 from .waist import compose_plan as _compose_plan
 from .jev_policy import aggregate_structural, policy_for
-<<<<<<< HEAD
 from .jev_completion import dogfood_phase
-=======
 from .jev_packs import validate_operator_pack
->>>>>>> origin/main
 from .capability import capabilities_payload as _capability_payload_owner
 from .brief import build_brief, validate_brief
 from .dag import TaskDAG, node_apply_kwargs
@@ -949,11 +946,8 @@ _DISPATCH = {
     "cost": _cmd_cost,
     "trust": _cmd_trust,
     "rankings": _cmd_rankings,
-<<<<<<< HEAD
     "jev-phase": _cmd_jev_phase,
-=======
     "mission": _cmd_mission,
->>>>>>> origin/main
 }
 
 
diff --git a/harness/cli_parser.py b/harness/cli_parser.py
index 1d3ffd7..8c1806c 100644
--- a/harness/cli_parser.py
+++ b/harness/cli_parser.py
@@ -342,7 +342,6 @@ def build_parser():
                       help="session cost ceiling for the live verify + apply phases")
     _add_output_flags(pdog)
 
-<<<<<<< HEAD
     pjphase = sub.add_parser(
         "jev-phase",
         help="Dogfood Jev 0-100 phase completion score; STATUS complete only "
@@ -359,8 +358,8 @@ def build_parser():
                          help="skip live Jev; use code gates + local semantic score")
     pjphase.add_argument("--json", action="store_true", help="emit raw JSON only")
     _add_output_flags(pjphase)
-=======
-    # HUL-A mission pack surface (run stubs until HUL-D driver lands).
+
+    # HUL-A mission pack surface.
     pmiss = sub.add_parser(
         "mission",
         help="Mission pack (HUL-A): init | status | resume | findings "
@@ -391,6 +390,5 @@ def build_parser():
         _p.add_argument("--root", default="missions",
                         help="pack parent directory (default: missions)")
         _add_output_flags(_p)
->>>>>>> origin/main
 
     return ap
