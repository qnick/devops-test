#!/bin/bash

source ./setenv.sh
envsubst < ./ansible/inventory.tpl
