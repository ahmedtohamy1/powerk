# Repository instructions

- Before local builds, read the CI workflow and required checks, inspect the
  current branch/PR's CI status and any failed job logs, and resolve known CI
  failures before treating a local build as validation.
- For API changes, check whether CI requires generated artifacts and regenerate
  and verify them before committing.
- After pushing, check CI for the exact pushed commit and report its actual
  status. Passing local tests or builds is not passing CI.
- Before changing any UI, read and follow [UI_GUIDELINES.md](UI_GUIDELINES.md).
  Use its semantic tokens and component rules. Update the guidelines and their
  automated checks together when a design rule changes.
- For web UI changes, run `node --test tests/*.test.cjs` and visually review the
  affected layouts and states. Do not disable the UI guardrails or add arbitrary
  component colors, type sizes, spacing, or corner radii.
