#!/bin/sh
set -eu

# Deliberately non-scoring: this task only provides an isolated workspace.
printf '0\n' > /logs/verifier/reward.txt
