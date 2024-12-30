#!/bin/bash

#If environment variable DEBUG is set to "true" bash debug will be set. Set Debug to "verbose" for more verbose debug information
if [ "${MY_DEBUG,,}" == "true" ]; then set -x; elif [ "${MY_DEBUG,,}" == "verbose" ]; then set -xv; fi

set -euo pipefail
IFS=$'\n\t'

echo "Magic before the APP"

envsubst </app/config.json.tpl >${APP_CONF:-/app/config.json}

#Execute the CMD entry from the Dockerfile
exec "$@"
