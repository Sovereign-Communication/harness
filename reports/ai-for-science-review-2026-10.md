# AI in Scientific Discovery, 2020–2026: Search, Prediction, and Verification

**A scoped review and claim audit**
**Evidence cut-off:** 28 October 2026
**Status:** Research draft for expert editorial review; not peer reviewed, exhaustive, or publication-ready.

## Abstract

Recent AI-for-science results span theorem and algorithm search, weather forecasting, plasma control, quantum chemistry, materials discovery, and protein design. Their common contribution is not the displacement of scientific method, but the expansion of search and prediction within systems whose goals, representations, constraints, and validation procedures remain substantially human-designed. Mathematics offers the clearest examples of machine-checkable outputs when a result is encoded in a formal system. In physical and biological sciences, benchmark skill and plausible predictions are useful but do not establish a mechanism, generalize beyond the evaluated distribution, or replace independent experiments. Several popular summaries overstate what the underlying work showed: GraphCast forecasts up to ten days at six-hour steps rather than at ten-minute resolution; FunSearch improved a lower bound for the cap-set problem rather than discovering the quoted upper bound; tokamak control transferred a simulation-trained policy to a real device; GNoME's predicted structures and A-Lab's synthesis results are separate datasets; and RFdiffusion generates backbones while sequence design is a distinct step. We conclude that the strongest emerging pattern is **AI-assisted search plus domain constraints plus independent validation**, with hybrid models and human-in-the-loop experiments remaining important. This is a selected scoping review, not a systematic census of every recent breakthrough.

## 1. Scope and method

This review examines selected high-signal primary studies from 2020 through the evidence cut-off, emphasizing mathematics and physics while including representative materials and life-science cases. The examples were selected to test claims made in a prior broad synthesis: that AI is discovering new mathematical structure, learning physical dynamics, and moving science from analysis to autonomous design. The review asks what each system actually produced, how it was evaluated, and which conclusions the evidence can support.

The review used model-confirmed query approval before web search. Nine initial specific topic queries and five narrower follow-up queries were approved; two broad searches for all 2025–2026 mathematics and physics breakthroughs were rejected. Only the approved queries were sent to search. The search pass prioritized primary journal or author sources, including papers and their supplements where available. Search results were leads, not evidence by themselves. Claims below are tied to the cited paper or its primary record.

This is a purposive scoping review, not a PRISMA-style systematic review or exhaustive list. It has no preregistered search protocol, independent dual screening, or complete citation database export. We manually checked the key claim-to-source relationships and corrected mismatches, but the Harness does not yet automate DOI resolution, field-by-field bibliography checks, or passage-level claim verification. Those missing product capabilities are tracked in [Harness issue #422](https://github.com/Sovereign-Communication/harness/issues/422). A human subject-matter editor should verify the final bibliography and scope before journal submission.

## 2. Mathematics: search can produce checkable objects, given a formal target

### 2.1 AlphaTensor: many decompositions, not a universal faster algorithm

AlphaTensor used reinforcement learning to search for tensor decompositions that implement matrix multiplication. Its best-known result concerns multiplication of 4×4 matrices over a finite field: it found a rank-47 decomposition, improving on the rank-49 construction obtained by applying Strassen's method twice. The system also found thousands of non-equivalent decompositions. These are algebraically checkable algorithmic objects, and the paper shows that some can be adapted to improve practical matrix-multiplication performance on particular hardware.

That result does not establish that all matrices have a new faster algorithm, that all discovered factorizations outperform existing code, or that the theoretical complexity of general matrix multiplication has been settled. “Unintuitive decompositions can be found by search” is supported; “the optimal algorithm for a mature problem has been overturned” is too broad. [Fawzi et al., *Nature* (2022)](https://doi.org/10.1038/s41586-022-05172-4).

### 2.2 FunSearch: program search improved a lower bound

FunSearch couples a language model that proposes programs with an evaluator and evolutionary selection. In the cap-set task, it found a construction of size 512 in the eight-dimensional vector space over the three-element field, improving the previous 496-point construction. The paper reports that this was a rare outcome among runs: 4 of 140 experiments found a 512-point set. It also improved the asymptotic lower bound for the cap-set capacity to 2.2202. The frequently repeated value 2.756 is an upper bound already known from prior work; it was not a FunSearch discovery.

For online bin packing, FunSearch produced heuristics that performed better than established baselines on the paper's tested benchmarks. This is evidence of constructive program search and benchmark improvement, not proof of performance in all workloads or production settings. Code can be checked deterministically, but the evaluator, objective, and formal interpretation of the output still define what has been proved. [Romera-Paredes et al., *Nature* (2024)](https://pmc.ncbi.nlm.nih.gov/articles/PMC10794145/).

### 2.3 AlphaGeometry and later formal proof search

AlphaGeometry solved 25 of 30 selected International Mathematical Olympiad geometry problems at a reported level comparable to a gold-medal performance. It combines a neural proposal model, synthetic training data, and symbolic deduction. Its result is notable because the final geometry derivations are symbolic and checkable. It does not show that the system independently chose the problems, invented the formal language, or can prove arbitrary mathematical statements. [Trinh et al., *Nature* (2024)](https://doi.org/10.1038/s41586-023-06747-5).

The 2026 AlphaProof Nexus manuscript reports Lean-verified solutions to subsets of Erdős problems and OEIS conjectures. At the evidence cut-off it is an arXiv preprint, not a peer-reviewed journal result. Formal verification establishes that a proof term follows from the formalized statement and library assumptions; it does not by itself establish that the original conjecture was correctly encoded, that the result is mathematically novel, or that the system independently formulated the question. The paper's user-supplied formalization step is part of the scientific workflow. Treat this as a frontier signal, not settled evidence of autonomous theorem discovery. [AlphaProof Nexus, arXiv preprint (2026)](https://arxiv.org/abs/2605.22763).

**Assessment.** In mathematics, AI can search enormous spaces of constructions and proofs and return objects that are mechanically checkable. This changes who or what can find a proof candidate. It does not remove the human roles of choosing a question, encoding definitions, checking novelty and significance, and deciding whether the formal result answers the intended mathematical question.

## 3. Physics: useful learned dynamics coexist with first-principles models

### 3.1 Weather prediction: GraphCast is a strong forecast system, not a replacement theorem

GraphCast is a learned global weather forecasting system trained on reanalysis data. The paper evaluates forecasts up to ten days ahead at 0.25° spatial resolution, using six-hour forecast steps. It outperformed the operational ECMWF HRES reference on about 90% of the reported verification targets. It can produce a ten-day forecast in under a minute on a TPU. “Ten-minute resolution” is an error: ten days is the forecast horizon, not a temporal step size.

These results establish benchmark skill for the tested period, variables, and metrics. They do not establish that operational forecasting no longer needs numerical weather prediction or that a learned model is superior for every lead time, variable, extreme event, or climate regime. Later comparisons illustrate this dependence on task and evaluation. A 2026 study of three flood-producing events found GraphCast strongest at a 1 mm/6 h rainfall threshold, while other systems led at higher thresholds; its event count was small, and one competing model had been fine-tuned on analyses spanning the test events. Those results are informative case studies, not a general ranking of weather systems. [Lam et al., *Science* (2023)](https://doi.org/10.1126/science.adi2336); [2026 flood-forecast comparison, *Geoscientific Model Development*](https://gmd.copernicus.org/articles/19/9831/2026/index.html).

NeuralGCM provides a direct counterexample to the claim that learned forecasts overturn first-principles modeling. It retains a differentiable dynamical core that solves large-scale atmospheric equations and learns parameterizations for unresolved processes. The authors report competitive weather forecasts, while also identifying limits on extrapolation to substantially different future climates. This hybrid design shows how learned components can complement physical structure where extrapolation and conservation matter. [NeuralGCM, *Nature* (2024)](https://pmc.ncbi.nlm.nih.gov/articles/PMC11357988/).

### 3.2 Tokamak control: simulation training followed by real-device demonstration

Deep reinforcement learning controlled plasma configurations in the TCV tokamak, including multiple plasma shapes. The policy was trained using a simulator and then transferred to the real device. This is a meaningful experimental demonstration of learned feedback control under difficult dynamics. Describing it as learning directly through unconstrained interaction with the reactor is misleading: simulation-to-device transfer and simulator mismatch are central parts of the work, and the authors discuss the risk of a controller specializing to its simulator. It demonstrates a useful control method under a defined setup, not that plasma behavior is too complex for engineering models or that safety and validation are solved. [Degrave et al., *Nature* (2022)](https://pmc.ncbi.nlm.nih.gov/articles/PMC8850200/).

### 3.3 Neural quantum states: a new ansatz, not a general solution to the many-body problem

FermiNet represents many-electron wavefunctions with neural networks and applies variational Monte Carlo to solve the electronic Schrödinger equation for selected atoms and molecules. The paper reports improvements over tested variational quantum Monte Carlo approaches and better dissociation curves than coupled-cluster calculations in some tested systems. This is an important alternative wavefunction ansatz. It does not solve the many-body problem for arbitrary systems, eliminate the need for physical equations, or establish universal chemical accuracy. [Pfau et al., *Physical Review Research* (2020)](https://doi.org/10.1103/PhysRevResearch.2.033429).

### 3.4 Learning a force law in dusty plasma: constraints and experiments remain central

A 2025 dusty-plasma study used three-dimensional particle trajectories to infer interparticle forces and reported deviations from commonly used theoretical assumptions. The method incorporated physical structure and constraints, and its predictions were compared with experimental measurements, including high reported agreement for the tested setup. This is stronger than a black-box curve fit, but the result remains specific to the measured system and chosen representation. It illustrates discovery from data assisted by physics-guided modeling, rather than the abandonment of theory. [Yu et al., *PNAS* (2025)](https://doi.org/10.1073/pnas.2505725122).

**Assessment.** Learned models can predict and control systems without numerically solving every governing equation at inference time. The evidence supports a broader modeling toolkit—learned surrogates, hybrid models, and learned controllers—not a general rule that first-principles equations are obsolete. Claims about extrapolation, mechanism, or safety require evidence beyond an in-distribution benchmark.

## 4. Materials and life sciences: generation narrows the search, experiments decide what works

### 4.1 GNoME predicts candidates; A-Lab is a separate synthesis study

GNoME used graph neural networks and active learning to search inorganic crystal structures. It reported 2.2 million new candidate structures, including 381,000 predicted to lie on the updated Materials Project convex hull. These are computational predictions and thermodynamic-screening results. “On the convex hull” is not equivalent to “experimentally synthesized,” “stable under all relevant conditions,” or “useful as a material.” The paper also reports independent experimental correspondence for a subset of the predictions.

The A-Lab paper is a separate autonomous synthesis study. Its targets were drawn from a broad set of computationally proposed materials and literature-informed candidate recipes, including cross-referencing the Materials Project and the DeepMind database. It reported 36 definitive synthesis successes among 57 targets; four more were inconclusive after manual X-ray diffraction review. A reported recipe-level success rate also depends on which denominator and confirmation rule is used. It is therefore inaccurate to say that A-Lab synthesized “41 GNoME predictions” at a verified 70% success rate. [GNoME, *Nature* (2023)](https://doi.org/10.1038/s41586-023-06735-9); [A-Lab, *Nature* (2023)](https://doi.org/10.1038/s41586-023-06734-w).

### 4.2 Protein design: generation and sequence design are distinct stages

RFdiffusion generates protein backbone structures under specified design tasks. A separate sequence-design method, ProteinMPNN, is then used to propose amino-acid sequences for those backbones; selected designs were tested experimentally. This workflow expands the range of structures scientists can propose and has produced experimentally validated examples. It does not mean RFdiffusion alone solves inverse folding in general or that most arbitrary generated proteins fold and function. The experimental success rate is conditional on task, filtering, and selection. [Watson et al., *Nature* (2023)](https://doi.org/10.1038/s41586-023-06415-8).

AlphaFold 2 achieved high accuracy on the CASP14 structure-prediction benchmark and transformed access to structural hypotheses. AlphaFold 3 extended prediction to complexes involving proteins, nucleic acids, ligands, ions, and other molecules. These systems predict structures; they do not close the sequence–structure problem for every protein or replace measurements of binding, dynamics, cellular context, or function. AlphaFold 3 has documented limitations, including stereochemical violations and variable performance across interaction classes. Predictions remain hypotheses to prioritize experiments. [Jumper et al., *Nature* (2021)](https://doi.org/10.1038/s41586-021-03819-2); [Abramson et al., *Nature* (2024)](https://doi.org/10.1038/s41586-024-07487-w).

### 4.3 Human-in-the-loop laboratories are an important counterexample to full autonomy

A 2026 SARA-H materials study reports a human-in-the-loop autonomous research system in which human guidance improved sampling efficiency, with robotic thin-film experiments on oxide systems. This does not diminish automation; it shows that experimental choice, scientific judgement, and machine execution can form a productive loop. The evidence does not support a simple transition from human intuition to fully autonomous discovery. [SARA-H, *PRX Intelligence* (published 10 September 2026)](https://doi.org/10.1103/c362-349b).

## 5. Cross-field pattern and what has—and has not—changed

Across these examples, AI most consistently changes the **search bottleneck**. It searches algorithmic decompositions, proposes programs, approximates expensive dynamics, explores candidate structures, or generates design hypotheses. The strongest workflows pair these proposals with a domain-defined objective and an independent verifier: a proof checker, a held-out benchmark, a physical device, a diffraction measurement, or a biochemical assay.

The evidence does not support a single overturned “mainstay” of science. It supports narrower revisions:

| Prior assumption | Evidence-based revision |
| --- | --- |
| Every useful forecast must be generated by a conventional numerical solver at runtime. | Learned models can provide high-skill forecasts for defined targets; hybrid models remain valuable, and performance is task- and distribution-dependent. |
| Search over mature mathematical objects is exhausted by human intuition. | Program and reinforcement-learning search can find new constructions and proof candidates, particularly when outputs can be mechanically checked. |
| Generative systems can directly design working physical or biological artifacts. | Generators can improve candidate search; synthesis, function, robustness, and reproducibility still need external validation. |
| Scientific verification always requires a human to inspect every derivation. | Formal proof systems can mechanically verify a proof relative to a formal statement and axioms; people still define and interpret the statement. |
| Autonomous discovery removes the scientist from the loop. | Human choices remain visible in goals, data, representations, constraints, safety criteria, experimental selection, and interpretation. |

The main change is a redistribution of scientific labor: less manual traversal of some search spaces, more attention to problem formulation, data provenance, model limitations, independent validation, and deciding which machine-generated candidate merits an experiment. “Prediction + search + validation” is a useful workflow description, but not a new scientific method that replaces observation, theory, or experiment.

## 6. Verification and limitations

The strength of evidence depends on the claim. A formal mathematical result can be highly reliable when the statement and proof are machine-checked, but encoding errors and hidden assumptions remain possible. Forecast skill is measured against specified baselines and datasets; temporal leakage, benchmark choice, rare-event coverage, calibration, and distribution shift matter. A laboratory demonstration establishes the measured result in that setup, not general success across a whole class of targets. A predicted structure or material remains a candidate until its relevant properties are observed.

Several conditions should accompany any claim of scientific discovery by AI:

1. **State the task and comparison precisely.** Report the benchmark, evaluation period, baseline, unit of analysis, and whether the result is a prediction, construction, proof, control action, or experimentally confirmed artifact.
2. **Separate stages.** Distinguish proposal generation, computational filtering, experimental selection, synthesis, and functional validation. Do not transfer a result from one stage to another.
3. **Report failures and denominators.** Show how many candidates or runs were attempted, selected, inconclusive, and confirmed. A best case is not a typical success rate.
4. **Test generalization and uncertainty.** Historical replay and benchmark success do not prove performance under novel regimes. Report calibration and out-of-distribution limitations where measured.
5. **Preserve the human and computational provenance.** Describe supplied formalizations, data sources, simulators, physical priors, search constraints, and expert interventions.
6. **Use “verified” narrowly.** A verified citation has correct bibliographic identity and supports the exact attached claim; a verified theorem is checked against an explicit formal statement; a validated scientific prediction has the required independent measurement. These are different standards.

This review is limited by its selected scope and manual evidence extraction. It does not estimate field-wide success rates, quantify compute concentration, or adjudicate the philosophical question of whether a model constitutes understanding. The original broad draft's claims about compute cost, reproducibility rates, and an “alien knowledge” crisis were not supported by the selected studies and are not repeated as findings here.

## 7. Conclusion

The recent record shows that AI can contribute genuinely new mathematical constructions, strong scientific predictions, improved candidate-generation systems, and useful closed-loop control. These are important advances. The most defensible cross-field trend is that machine learning and search enlarge the set of hypotheses and solutions scientists can examine, while formal, computational, and experimental checks determine which outputs count as knowledge. First-principles reasoning, human judgement, and empirical validation have not been overturned. Their roles are changing and, in several successful systems, are built directly into the architecture or workflow.

This paper should therefore be treated as a sourced review draft for expert review. It is not a “verified publishable paper”: publication readiness would require an agreed journal and scope, reproducible systematic-search protocol, complete reference metadata audit, independent subject-matter review, and editorial revision.

## References

1. Pfau, D. et al. “Ab initio solution of the many-electron Schrödinger equation with deep neural networks.” *Physical Review Research* (2020). [DOI](https://doi.org/10.1103/PhysRevResearch.2.033429).
2. Jumper, J. et al. “Highly accurate protein structure prediction with AlphaFold.” *Nature* (2021). [DOI](https://doi.org/10.1038/s41586-021-03819-2).
3. Degrave, J. et al. “Magnetic control of tokamak plasmas through deep reinforcement learning.” *Nature* (2022). [DOI](https://doi.org/10.1038/s41586-021-04301-9).
4. Fawzi, A. et al. “Discovering faster matrix multiplication algorithms with reinforcement learning.” *Nature* (2022). [DOI](https://doi.org/10.1038/s41586-022-05172-4).
5. Lam, R. et al. “Learning skillful medium-range global weather forecasting.” *Science* (2023). [DOI](https://doi.org/10.1126/science.adi2336).
6. Merchant, A. et al. “Scaling deep learning for materials discovery.” *Nature* (2023). [DOI](https://doi.org/10.1038/s41586-023-06735-9).
7. Szymanski, N. J. et al. “An autonomous laboratory for the accelerated synthesis of inorganic materials.” *Nature* (2023). [DOI](https://doi.org/10.1038/s41586-023-06734-w).
8. Watson, J. L. et al. “De novo design of protein structure and function with RFdiffusion.” *Nature* (2023). [DOI](https://doi.org/10.1038/s41586-023-06415-8).
9. Trinh, T. H. et al. “Solving olympiad geometry without human demonstrations.” *Nature* (2024). [DOI](https://doi.org/10.1038/s41586-023-06747-5).
10. Romera-Paredes, B. et al. “Mathematical discoveries from program search with large language models.” *Nature* (2024). [Primary article](https://pmc.ncbi.nlm.nih.gov/articles/PMC10794145/).
11. Abramson, J. et al. “Accurate structure prediction of biomolecular interactions with AlphaFold 3.” *Nature* (2024). [DOI](https://doi.org/10.1038/s41586-024-07487-w).
12. “Neural general circulation models for weather and climate.” *Nature* (2024). [Primary article](https://pmc.ncbi.nlm.nih.gov/articles/PMC11357988/).
13. Yu, C. et al. Dusty-plasma force-law discovery study. *Proceedings of the National Academy of Sciences* (2025). [DOI](https://doi.org/10.1073/pnas.2505725122).
14. “AlphaProof Nexus.” arXiv preprint (2026). [arXiv:2605.22763](https://arxiv.org/abs/2605.22763).
15. SARA-H human-in-the-loop materials study. *PRX Intelligence* (published 10 September 2026). [DOI](https://doi.org/10.1103/c362-349b).
16. 2026 flood-forecast comparison. *Geoscientific Model Development* 19, 9831 (2026). [Article](https://gmd.copernicus.org/articles/19/9831/2026/index.html).
