# Tarantool Cloud

Tarantool Cloud is a service that allows you to run a self-healing, replicated set of tarantool instances. It is built on top of Consul and is resilient to some of the typical hardware and network problems.

It provides the following key features:

- **REST API**: for programmatic creation and destruction of tarantool instances
- **WEB UI**: to allow human agents create single on-demand instances and get an overview of the system
- **Failover**: to automatically recover from node failures or degraded storage

Read more on the wiki: [Documentation](https://github.com/tarantool/cloud/wiki)

## Requirements

At least two virtual or physical machines with ubuntu 16 and python 2.7 installed
Each box should be accessible via ssh

Ansible 2.1 or greater on the host box

## Getting Started

- To prepare an environment, do:

```sh
cp setenv.sh.tpl setenv.sh
```
- Then edit file setenv.sh, fill it up with ip addresses of your boxes, credentials and some secure stuff.

Tokens can be generated with:
```sh
uuidgen -r
```
Key can be generated with:
```sh
openssl rand 16 | base64
```

- Now you can generate inventory file for ansible
```sh
./create_inventory.sh > ansible/inventory.cfg
```

- And run ansible-playbook
```sh
cd ansible
ansible-playbook -i inventory.cfg site.yml
```

- Wait about 30 minutes until ansible-playbook is stopped.

- And your cluster is ready, you can now create tarantool-memcached instances:

```sh
./taas -H $HOST1_IP:5060 -v run --name=test1 --memsize=100
```
*Note*: first-time creation and launch of tarantool instances may take a very long time, as the instance manager is building docker images.

- List all instances with command:
```sh
./taas -H $HOST1_IP:5060  ps
```

- Remove instance:
```sh
./taas -H $HOST1_IP:5060  rm $INSTANCE_ID
```
You can use either '$HOST2_IP', the result will be the same.

## Health checking

For health checking you can use consul web interface http://HOST_IP:8500 or cloud_api web interface http://$HOST_IP:5060 on any host


## Connecting to instances

- First you should get correct port numbers:
```sh
./getports.sh $INSTANCE_ID
```

- You should get answer looking like this:
```sh
192.168.124.179 32768
192.168.124.25 32768
```
Ip addresses and port numbers shouldn't be the same.

- Then you can send something to the first address
```sh
$ printf "set key 0 60 5\r\nvalue\r\n" | nc 192.168.124.179 32768
STORED
```

- And get it from the second:
```sh
$ printf "get key\r\n" | nc 192.168.124.25 32768
VALUE key 0 5
value
END
```

## Add new node to the cluster

- Edit ansible/inventory.cfg and uncomment line with 'node3'.
- Run ansible-playbook again
```sh
cd ansible
ansible-playbook -i inventory.cfg site.yml
```

## License

BSD (see LICENSE file)
