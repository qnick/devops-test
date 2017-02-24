#!/bin/bash

source ./setenv.sh

TIMEOUT=5
RESP=$(curl -m $TIMEOUT -s http://$HOST1_IP:8500/v1/catalog/service/${1}?token=$CONSUL_TOKEN)

echo ${RESP} | python -c 'import sys, json; j=json.load(sys.stdin); print j[0]["ServiceAddress"]+" "+str(j[0]["ServicePort"])'
echo ${RESP} | python -c 'import sys, json; j=json.load(sys.stdin); print j[1]["ServiceAddress"]+" "+str(j[1]["ServicePort"])'
