#!/bin/bash
while read line; do
  docker exec clab-srl-evpn-topo-spine1 sr_cli -c "enter candidate" -c "$line" > /dev/null 2>&1
  if [ $? -ne 0 ]; then
    echo "FAILED: $line"
  fi
done < configs/spine1.cfg
