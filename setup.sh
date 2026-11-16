#!/bin/sh
# acid-casino - Developed by acidvegas in Python (https://github.com/acidvegas)
# setup.sh

set -e
ARCADE=''
PUBLIC_URL=''
if [ -n "$CASINO_PUBLIC_URL" ]; then
	PUBLIC_URL='-e CASINO_PUBLIC_URL'
fi
if [ -n "$ARCADE_URL" ]; then
	PUBLIC='https://arcade.{domain}' # {domain} is filled in with the player's parent domain
	ARCADE="--network arcade -e ARCADE_URL -e ARCADE_PUBLIC_URL=${ARCADE_PUBLIC_URL:-$PUBLIC} -e ARCADE_GAME=casino -e ARCADE_KEY"
fi
docker build -t acidcasino .
docker rm -f acidcasino 2>/dev/null || true
docker run -d \
	--name acidcasino \
	--restart unless-stopped \
	-p ${CASINO_BIND:-127.0.0.1}:20005:20005 \
	-e CASINO_PROXIES=${CASINO_PROXIES:-0} \
	-v acidcasino_data:/data \
	$PUBLIC_URL \
	$ARCADE \
	acidcasino
