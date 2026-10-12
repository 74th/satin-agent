#!/bin/sh
# Sample startup.sh. Runs before the worker on every start, with /workspace as cwd.
# Env: SATIN_MODE, SATIN_SESSION_ID, SATIN_FIRST_RUN (1|0), AGENT_USER_EMAIL.
# A non-zero exit stops the worker unless SATIN_STARTUP_CONTINUE_ON_FAILURE=1.
echo "satin startup: mode=$SATIN_MODE session=$SATIN_SESSION_ID first_run=$SATIN_FIRST_RUN"
