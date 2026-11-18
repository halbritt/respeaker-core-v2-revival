#!/bin/sh
# Run a command in the armhf cross container with this directory's src/ and out/ mounted.
#   ./cross.sh bash                      interactive shell
#   ./cross.sh ./build-odas.sh           build ODAS (see that script)
#   ./cross.sh ./build-aec.sh            build rsp-aec, the echo canceller (see that script)
# board/ is mounted read-only at /board for the tools whose source lives there.
# The user is not in the docker group, hence sudo -g docker.
here=$(cd "$(dirname "$0")" && pwd)
exec sudo -g docker docker run --rm $( [ -t 0 ] && echo -it ) -u "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$here":/work -v "$here/../../board":/board:ro rsp-cross "$@"
