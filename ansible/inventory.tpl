[ca]
localhost

[ca:vars]
tarantool_cloud_tls_ca_key_password=$CA_KEY_PASSWORD
ansible_connection=ssh
ansible_user=$LOCALHOST_USER
ansible_ssh_pass=$LOCALHOST_PASSWORD
ansible_become=false
#ansible_become_user=root
        
[cloud-node]
node1 ansible_host=$HOST1_IP  consul_server=true
node2 ansible_host=$HOST2_IP  consul_server=true
#node3 ansible_host=$HOST3_IP  consul_server=true

[cloud-node:vars]
ansible_connection=ssh
ansible_user=$REMOTE_USER
ansible_ssh_pass=$REMOTE_PASSWORD
ansible_become=true
ansible_become_user=root
ansible_become_pass=$REMOTE_PASSWORD
consul_version=0.7.5
consul_bootstrap_expect=2
consul_acl_master_token=$CONSUL_MASTER_TOKEN
consul_acl_token=$CONSUL_TOKEN
consul_gossip_key=$CONSUL_KEY
