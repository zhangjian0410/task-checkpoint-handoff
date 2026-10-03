# Capability maintenance

Read README.md and the matched SKILL.md/references before changing behavior. Keep the host's instructions and target project's rules in force.

Maintain the Skill contract, helper and black-box tests together. Core code depends on Python/Git and explicit paths; host discovery metadata stays optional. Keep real task data, credentials and run artifacts outside this source repository.

Run `python3 -B -m unittest discover -s tests` after behavior changes. Preserve schema-v1 evidence and index compatibility. Changing output selection must not rewrite sealed checkpoints. A test passing locally is not proof of native discovery or another Harness's end-to-end behavior.
