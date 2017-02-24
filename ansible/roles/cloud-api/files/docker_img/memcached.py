#!/usr/bin/env python3
# pylint: disable=missing-super-argument
import os
import global_env
import group
import consul
from sense import Sense
import ip_pool
import random
import logging
import docker
import uuid
import time
import tarantool
import allocate
import datetime
import json
import task
import tarfile
import base64
import gzip
import io
import shutil

class MemcachedTask(task.Task):
    memcached_task_type = None
    def __init__(self, group_id):
        super().__init__(self.memcached_task_type)
        self.group_id = group_id

    def get_dict(self, index=None):
        obj = super().get_dict(index)
        obj['group_id'] = self.group_id
        return obj


class CreateTask(MemcachedTask):
    memcached_task_type = "create_memcached"


class UpdateTask(MemcachedTask):
    memcached_task_type = "update_memcached"


class DeleteTask(MemcachedTask):
    memcached_task_type = "delete_memcached"


class BackupTask(MemcachedTask):
    def __init__(self, group_id, backup_id):
        super().__init__(group_id)
        self.backup_id = backup_id
    memcached_task_type = "backup_memcached"


class RestoreTask(MemcachedTask):
    def __init__(self, group_id, restore_id):
        super().__init__(group_id)
        self.restore_id = restore_id
    memcached_task_type = "restore_memcached"


def backup_is_valid(storage, digest):
    return True


class Memcached(group.Group):
    def __init__(self, consul_host, group_id):
        super(Memcached, self).__init__(consul_host, group_id)

    @classmethod
    def get(cls, group_id):
        memc = Memcached(global_env.consul_host, group_id)

        return memc

    @classmethod
    def create(cls, create_task, name, memsize, password, check_period):
        group_id = create_task.group_id

        try:
            consul_obj = consul.Consul(host=global_env.consul_host,
                                       token=global_env.consul_acl_token)
            kv = consul_obj.kv

            create_task.log("Creating group '%s'", group_id)

            ip1 = ip_pool.allocate_ip()
            ip2 = ip_pool.allocate_ip()
            creation_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

            kv.put('tarantool/%s/blueprint/type' % group_id, 'memcached')
            kv.put('tarantool/%s/blueprint/name' % group_id, name.encode('utf-8'))
            kv.put('tarantool/%s/blueprint/memsize' % group_id, str(memsize))
            kv.put('tarantool/%s/blueprint/check_period' % group_id, str(check_period))
            kv.put('tarantool/%s/blueprint/creation_time' % group_id, creation_time)
            kv.put('tarantool/%s/blueprint/instances/1/addr' % group_id, ip1)
            kv.put('tarantool/%s/blueprint/instances/2/addr' % group_id, ip2)

            Sense.update()

            memc = Memcached(global_env.consul_host, group_id)

            create_task.log("Allocating instance to physical nodes")

            memc.allocate()
            Sense.update()

            create_task.log("Registering services")
            memc.register()
            Sense.update()

            create_task.log("Creating containers")
            memc.create_containers(password)
            Sense.update()

            create_task.log("Enabling replication")
            memc.wait_for_instances(create_task)
            memc.enable_replication()

            create_task.log("Completed creating group")

            create_task.set_status(task.STATUS_SUCCESS)
        except Exception as ex:
            logging.exception("Failed to create group '%s'", group_id)
            create_task.set_status(task.STATUS_CRITICAL, str(ex))

            raise

        return memc

    def delete(self, delete_task):
        try:
            group_id = self.group_id

            delete_task.log("Unallocating instance")
            self.unallocate()

            delete_task.log("Unregistering services")
            self.unregister()

            delete_task.log("Removing containers")
            self.remove_containers()

            delete_task.log("Removing blueprint")
            self.remove_blueprint()

            delete_task.log("Completed removing group")

            Sense.update()
            delete_task.set_status(task.STATUS_SUCCESS)
        except Exception as ex:
            logging.exception("Failed to delete group '%s'", group_id)
            delete_task.set_status(task.STATUS_CRITICAL, str(ex))

            raise

    def upgrade(self, upgrade_task):
        try:
            group_id = self.group_id

            upgrade_task.log("Upgrading container 1")
            self.upgrade_container("1")

            upgrade_task.log("Upgrading container 2")
            self.upgrade_container("2")

            upgrade_task.log("Completed upgrading containers")

            Sense.update()
            upgrade_task.set_status(task.STATUS_SUCCESS)
        except Exception as ex:
            logging.exception("Failed to upgrade group '%s'", group_id)
            upgrade_task.set_status(task.STATUS_CRITICAL, str(ex))

            raise

    def update(self, name, memsize, password,
               docker_image_name, heal, backup_id, storage, update_task):
        try:
            if heal:
                self.heal(update_task)

            if name and name != self.blueprint['name']:
                self.rename(name, update_task)

            if memsize and memsize != self.blueprint['memsize']:
                self.resize(memsize, update_task)

            if password:
                self.set_password(password, update_task)

            if docker_image_name:
                self.upgrade(update_task)

            if backup_id:
                self.restore(backup_id, storage, update_task)

            Sense.update()
            update_task.set_status(task.STATUS_SUCCESS)
        except Exception as ex:
            logging.exception("Failed to update group '%s'", self.group_id)
            update_task.set_status(task.STATUS_CRITICAL, str(ex))

            raise

    def heal(self, update_task):
        blueprint = self.blueprint
        allocation = self.allocation
        containers = self.containers

        if len(containers['instances']) == 2:
            update_task.log("All containers are present. No need to heal.")
            return

        if len(containers['instances']) == 0:
            update_task.log("No live containers. Can't heal.")
            raise RuntimeError("No live containers")

        instance_num = str(3-int(list(containers['instances'].keys())[0]))
        other_instance_num = list(containers['instances'].keys())[0]
        password_base64 = self.get_instance_password(other_instance_num)
        update_task.log("Re-creating container %s from %s",
                        instance_num,
                        other_instance_num)
        if password_base64 is not None:
            update_task.log("Will set password for %s", instance_num)

        update_task.log("Unregistering container %s", instance_num)
        self.unregister_instance(instance_num)

        update_task.log("Disconnecting container %s", instance_num)
        self.disconnect_instance(instance_num)

        self.create_container(instance_num, other_instance_num,
                              password=None,
                              password_base64=password_base64)

        update_task.log("Registring container %s", instance_num)
        self.register_instance(instance_num)





    def rename(self, name, update_task):
        consul_obj = consul.Consul(host=global_env.consul_host,
                                   token=global_env.consul_acl_token)
        kv = consul_obj.kv

        msg = "Renaming group '%s' to '%s'" % (self.group_id, name)
        update_task.log(msg)
        logging.info(msg)

        kv.put('tarantool/%s/blueprint/name' % self.group_id, name)

    def resize(self, memsize, update_task):
        consul_obj = consul.Consul(host=global_env.consul_host,
                                   token=global_env.consul_acl_token)
        kv = consul_obj.kv

        update_task.log("Resizing instance 1")
        self.resize_instance("1", memsize)
        update_task.log("Resizing instance 2")
        self.resize_instance("2", memsize)

        kv.put('tarantool/%s/blueprint/memsize' % self.group_id, str(memsize))
        update_task.log("Completed resizing")

    def set_password(self, password, update_task):
        update_task.log("Setting password for instance 1")
        self.set_instance_password("1", password)
        update_task.log("Setting password for instance 2")
        self.set_instance_password("2", password)

    def allocate(self):
        consul_obj = consul.Consul(host=global_env.consul_host,
                                   token=global_env.consul_acl_token)
        kv = consul_obj.kv

        blueprint = self.blueprint

        host1 = allocate.allocate(blueprint['memsize'])
        host2 = allocate.allocate(blueprint['memsize'], anti_affinity=[host1])

        kv.put('tarantool/%s/allocation/instances/1/host' %
               self.group_id, host1)
        kv.put('tarantool/%s/allocation/instances/2/host' %
               self.group_id, host2)

    def unallocate(self):
        consul_obj = consul.Consul(host=global_env.consul_host,
                                   token=global_env.consul_acl_token)
        kv = consul_obj.kv

        logging.info("Unallocating '%s'", self.group_id)

        kv.delete("tarantool/%s/allocation" % self.group_id,
                  recurse=True)

    def register(self):
        self.register_instance("1")
        self.register_instance("2")

    def unregister(self):
        self.unregister_instance("1")
        self.unregister_instance("2")

    def backup(self, backup_task, storage):
        try:
            services = self.services
            backup_id = backup_task.backup_id
            group_id = self.group_id

            backup_task.log("Backing up group '%s'", group_id)

            instance_num = '1'

            allocation = self.allocation
            instance_id = self.group_id + '_' + instance_num
            docker_host = allocation['instances'][instance_num]['host']
            docker_hosts = Sense.docker_hosts()

            docker_addr = None
            for host in docker_hosts:
                if host['addr'].split(':')[0] == docker_host or \
                   host['consul_host'] == docker_host:
                    docker_addr = host['addr']

            if not docker_addr:
                raise RuntimeError("No such Docker host: '%s'" % docker_host)

            docker_obj = docker.APIClient(base_url=docker_addr,
                                       tls=global_env.docker_tls_config)

            cmd = 'ls /var/lib/tarantool'
            exec_id = docker_obj.exec_create(self.group_id + '_' + instance_num,
                                         cmd)
            out = docker_obj.exec_start(exec_id)
            ret = docker_obj.exec_inspect(exec_id)

            if ret['ExitCode'] != 0:
                raise RuntimeError("Failed to list snapshots for container " +
                                   instance_id)

            files = out.decode('utf-8').split('\n')
            snapshots = [f for f in files if f.endswith('.snap')]
            snapshot_lsns = sorted([os.path.splitext(s)[0] for s in snapshots])
            xlogs = [f for f in files if f.endswith('.xlog')]
            xlog_lsns = sorted([os.path.splitext(s)[0] for s in xlogs])

            if not snapshot_lsns:
                raise RuntimeError("There are no snapshots to backup")

            latest_snapshot_lsn = snapshot_lsns[-1]

            older_xlogs = list(filter(
                lambda x: x <= latest_snapshot_lsn, xlog_lsns))
            older_xlog = older_xlogs[-1]

            newer_xlogs = list(filter(
                lambda x: x > latest_snapshot_lsn, xlog_lsns))

            xlogs_to_backup = [older_xlog] + newer_xlogs

            files_to_backup = [latest_snapshot_lsn + '.snap']
            files_to_backup += [xlog + '.xlog' for xlog in xlogs_to_backup]

            backup_task.log("Backing up data: %s", ', '.join(files_to_backup))

            tmp_backup_dir = '/var/lib/tarantool/backup-' + uuid.uuid4().hex

            cmd = "mkdir '%s'" % tmp_backup_dir
            exec_id = docker_obj.exec_create(self.group_id + '_' + instance_num,
                                             cmd)
            out = docker_obj.exec_start(exec_id)
            ret = docker_obj.exec_inspect(exec_id)

            if ret['ExitCode'] != 0:
                raise RuntimeError(
                    "Failed to create temp backup dir for container " +
                    instance_id)

            for file_to_backup in files_to_backup:
                cmd = "ln /var/lib/tarantool/%s %s/%s" % (
                    file_to_backup, tmp_backup_dir, file_to_backup)
                exec_id = docker_obj.exec_create(
                    self.group_id + '_' + instance_num, cmd)
                out = docker_obj.exec_start(exec_id)
                ret = docker_obj.exec_inspect(exec_id)

                if ret['ExitCode'] != 0:
                    raise RuntimeError(
                        "Failed to hardlink backup file: " + out.decode('utf-8'))

            strm, _ = docker_obj.get_archive(instance_id, tmp_backup_dir+'/.')
            archive_id, size = storage.put_archive(strm)

            cmd = "rm -rf /var/lib/tarantool/backup-*"
            exec_id = docker_obj.exec_create(self.group_id + '_' + instance_num,
                                             cmd)
            out = docker_obj.exec_start(exec_id)
            ret = docker_obj.exec_inspect(exec_id)

            if ret['ExitCode'] != 0:
                raise RuntimeError(
                    "Failed to remove temp backup dir for container " +
                    instance_id)

            mem_used = services['instances'][instance_num]['mem_used']
            storage.register_backup(backup_id, archive_id, group_id,
                                    'memcached', size, mem_used)

            Sense.update()

            backup_task.set_status(task.STATUS_SUCCESS)
        except Exception as ex:
            logging.exception("Failed to backup '%s'", group_id)
            backup_task.set_status(task.STATUS_CRITICAL, str(ex))

    def restore(self, backup_id, storage, restore_task):
        blueprint = self.blueprint
        services = self.services
        group_id = self.group_id

        restore_task.log("Restoring group '%s'", group_id)

        backup = Sense.backups()[backup_id]
        archive_id = backup['archive_id']
        mem_used = backup['mem_used']

        try:
            for instance_num in ('1', '2'):
                allocation = self.allocation
                instance_id = self.group_id + '_' + instance_num
                docker_host = allocation['instances'][instance_num]['host']
                docker_hosts = Sense.docker_hosts()

                restore_task.log("Restoring instance: '%s'", instance_id)

                docker_addr = None
                for host in docker_hosts:
                    if host['addr'].split(':')[0] == docker_host or \
                       host['consul_host'] == docker_host:
                        docker_addr = host['addr']

                if not docker_addr:
                    raise RuntimeError("No such Docker host: '%s'" % docker_host)

                docker_obj = docker.APIClient(base_url=docker_addr,
                                           tls=global_env.docker_tls_config)

                if mem_used > blueprint['memsize']:
                    err = ("Backed up instance used {} MiB of RAM, but " +
                           "instance {} only has {} MiB max").format(
                               mem_used, group_id, blueprint['memsize'])
                    restore_task.set_status(task.STATUS_CRITICAL, err)
                    return

                tmp_restore_dir = '/var/lib/tarantool/restore-' + uuid.uuid4().hex

                cmd = "mkdir '%s'" % tmp_restore_dir
                exec_id = docker_obj.exec_create(
                    self.group_id + '_' + instance_num, cmd)
                out = docker_obj.exec_start(exec_id)
                ret = docker_obj.exec_inspect(exec_id)

                if ret['ExitCode'] != 0:
                    raise RuntimeError(
                        "Failed to create temp restore dir for container " +
                        instance_id + ": " + out.decode('utf-8'))

                stream = storage.get_archive(archive_id)

                docker_obj.put_archive(instance_id, tmp_restore_dir, stream)

                cmd = "sh -c 'rm -rf /var/lib/tarantool/*.snap'"
                exec_id = docker_obj.exec_create(
                    self.group_id + '_' + instance_num, cmd)
                out = docker_obj.exec_start(exec_id)
                ret = docker_obj.exec_inspect(exec_id)

                if ret['ExitCode'] != 0:
                    raise RuntimeError(
                        "Failed to remove existing snap files of " +
                        instance_id + ": " + out.decode('utf-8'))

                cmd = "sh -c 'rm -rf /var/lib/tarantool/*.xlog'"
                exec_id = docker_obj.exec_create(
                    self.group_id + '_' + instance_num, cmd)
                out = docker_obj.exec_start(exec_id)
                ret = docker_obj.exec_inspect(exec_id)

                if ret['ExitCode'] != 0:
                    raise RuntimeError(
                        "Failed to remove existing xlog files of " +
                        instance_id + ": " + out.decode('utf-8'))

                cmd = "sh -c 'mv %s/* /var/lib/tarantool'" % tmp_restore_dir
                exec_id = docker_obj.exec_create(
                    self.group_id + '_' + instance_num, cmd)
                out = docker_obj.exec_start(exec_id)
                ret = docker_obj.exec_inspect(exec_id)

                if ret['ExitCode'] != 0:
                    raise RuntimeError(
                        "Failed to restore files of" +
                        instance_id + ": " + out.decode('utf-8'))

                cmd = "rm -rf '%s'" % tmp_restore_dir
                exec_id = docker_obj.exec_create(
                    self.group_id + '_' + instance_num, cmd)
                out = docker_obj.exec_start(exec_id)
                ret = docker_obj.exec_inspect(exec_id)

                if ret['ExitCode'] != 0:
                    raise RuntimeError(
                        "Failed to remove tmp restore dir of " +
                        instance_id + ": " + out.decode('utf-8'))


                restore_task.log("Restarting instance: '%s'", instance_id)
                docker_obj.restart(container=instance_id)

            restore_task.log("Enabling replication")
            self.wait_for_instances(restore_task)
            self.enable_replication()
        except Exception as ex:
            logging.exception("Failed to restore backup '%s'", group_id)
            restore_task.set_status(task.STATUS_CRITICAL, str(ex))


    def create_containers(self, password):
        self.create_container("1", None, password, None)
        self.create_container("2", "1", password, None)

    def remove_containers(self):
        self.remove_container("1")
        self.remove_container("2")

    def remove_blueprint(self):
        consul_obj = consul.Consul(host=global_env.consul_host,
                                   token=global_env.consul_acl_token)
        kv = consul_obj.kv

        logging.info("Removing blueprint '%s'", self.group_id)

        kv.delete("tarantool/%s/blueprint" % self.group_id,
                  recurse=True)

    def wait_for_instances(self, wait_task):
        port = 3301

        blueprint = self.blueprint
        allocation = self.allocation

        for instance_num in allocation['instances']:
            other_instances = \
                set(allocation['instances'].keys()) - set([instance_num])

            addr = blueprint['instances'][instance_num]['addr']
            other_addrs = [blueprint['instances'][i]['addr']
                           for i in other_instances]
            docker_host = allocation['instances'][instance_num]['host']
            docker_hosts = Sense.docker_hosts()
            instance_id = self.group_id + '_' + instance_num

            wait_task.log("Waiting for '%s' to go up. It may take time to " +
                          "load data from disk.", instance_id)

            docker_addr = None
            for host in docker_hosts:
                if host['addr'].split(':')[0] == docker_host or \
                   host['consul_host'] == docker_host:
                    docker_addr = host['addr']

            docker_obj = docker.APIClient(base_url=docker_addr,
                                       tls=global_env.docker_tls_config)

            cmd = "tarantool_is_up"
            attempts = 0
            while True:
                exec_id = docker_obj.exec_create(instance_id,
                                                 cmd)
                stream = docker_obj.exec_start(exec_id, stream=True)

                for line in stream:
                    logging.info("Exec: %s", str(line))

                ret = docker_obj.exec_inspect(exec_id)

                if ret['ExitCode'] == 0:
                    break

                wait_task.log("Waiting for '%s' to go up. Attempt %d.",
                              instance_id, attempts)

                time.sleep(1)
                attempts += 1


    def enable_replication(self):
        port = 3301

        blueprint = self.blueprint
        allocation = self.allocation

        for instance_num in allocation['instances']:
            other_instances = \
                set(allocation['instances'].keys()) - set([instance_num])

            addr = blueprint['instances'][instance_num]['addr']
            other_addrs = [blueprint['instances'][i]['addr']
                           for i in other_instances]
            docker_host = allocation['instances'][instance_num]['host']
            docker_hosts = Sense.docker_hosts()

            logging.info("Enabling replication between '%s' and '%s'",
                         addr, str(other_addrs))

            docker_addr = None
            for host in docker_hosts:
                if host['addr'].split(':')[0] == docker_host or \
                   host['consul_host'] == docker_host:
                    docker_addr = host['addr']


            docker_obj = docker.APIClient(base_url=docker_addr,
                                       tls=global_env.docker_tls_config)

            cmd = "tarantool_set_config.lua TARANTOOL_REPLICATION_SOURCE " + \
                  ",".join(other_addrs)

            attempts = 0
            while attempts < 5:
                exec_id = docker_obj.exec_create(self.group_id + '_' + instance_num,
                                                 cmd)
                stream = docker_obj.exec_start(exec_id, stream=True)

                for line in stream:
                    logging.info("Exec: %s", str(line))

                ret = docker_obj.exec_inspect(exec_id)

                if ret['ExitCode'] == 0:
                    break

                time.sleep(1)
                attempts+=1

            if attempts >= 5:
                raise RuntimeError("Failed to enable replication for group " +
                                   self.group_id)


    def register_instance(self, instance_num):
        blueprint = self.blueprint
        allocation = self.allocation

        instance_id = self.group_id + '_' + instance_num
        docker_host = allocation['instances'][instance_num]['host']
        docker_hosts = Sense.docker_hosts()
        consul_host = None
        for host in docker_hosts:
            if host['addr'].split(':')[0] == docker_host or \
               host['consul_host'] == docker_host:
                consul_host = host['consul_host']
        if not consul_host:
            raise RuntimeError("Failed to find consul host of %s" % docker_host)

        addr = blueprint['instances'][instance_num]['addr']
        check_period = blueprint['check_period']

        consul_obj = consul.Consul(host=consul_host,
                                   token=global_env.consul_acl_token)

        replication_check = {
            'docker_container_id': instance_id,
            'shell': "/bin/sh",
            'script': "/var/lib/mon.d/tarantool_replication.sh",
            'interval': "%ds" % check_period,
            'status': 'warning'
        }

        memory_check = {
            'docker_container_id': instance_id,
            'shell': "/bin/sh",
            'script': "/var/lib/mon.d/tarantool_memory.sh",
            'interval': "%ds" % check_period,
            'status': 'warning'
        }

        logging.info("Registering instance '%s' on '%s'",
                     instance_id,
                     consul_host)

        ret = consul_obj.agent.service.register("memcached",
                                                service_id=instance_id,
                                                address=addr,
                                                port=11211,
                                                check=replication_check,
                                                tags=['tarantool'])

        ret = consul_obj.agent.check.register("Memory Utilization",
                                              check=memory_check,
                                              check_id=instance_id + '_memory',
                                              service_id=instance_id)

    def disconnect_instance(self, instance_num):
        blueprint = self.blueprint
        allocation = self.allocation

        instance_id = self.group_id + '_' + instance_num
        addr = blueprint['instances'][instance_num]['addr']
        memsize = blueprint['memsize']
        network_settings = Sense.network_settings()
        network_name = network_settings['network_name']
        if not network_name:
            raise RuntimeError("Network name is not specified in settings")

        docker_host = allocation['instances'][instance_num]['host']
        docker_hosts = Sense.docker_hosts()

        docker_addr = None
        for host in docker_hosts:
            if host['addr'].split(':')[0] == docker_host or \
               host['consul_host'] == docker_host:
                docker_addr = host['addr']

        if not docker_addr:
            raise RuntimeError("No such Docker host: '%s'" % docker_host)

        docker_obj = docker.APIClient(base_url=docker_addr,
                                   tls=global_env.docker_tls_config)

        try:
            docker_obj.disconnect_container_from_network(instance_id,
                                                         network_name,
                                                         force=True)
        except:
            pass

    def unregister_instance(self, instance_num):
        services = self.services
        allocation = self.allocation

        if instance_num not in services['instances']:
            return

        instance_id = self.group_id + '_' + instance_num

        docker_host = allocation['instances'][instance_num]['host']
        docker_hosts = Sense.docker_hosts()
        consul_host = None
        for host in docker_hosts:
            if host['addr'].split(':')[0] == docker_host or \
               host['consul_host'] == docker_host:
                consul_host = host['consul_host']
        if not consul_host:
            raise RuntimeError("Failed to find consul host of %s" % docker_host)

        consul_hosts = [h['addr'].split(':')[0] for h in Sense.consul_hosts()
                        if h['status'] == 'passing']

        if services:
            if consul_host in consul_hosts:
                consul_obj = consul.Consul(host=consul_host,
                                           token=global_env.consul_acl_token)

                check_id = instance_id + '_memory'
                logging.info("Unregistering check '%s'", check_id)
                consul_obj.agent.check.deregister(check_id)
                consul_obj.agent.check.deregister('service:'+instance_id)

                logging.info("Unregistering instance '%s' from '%s'",
                             instance_id,
                             consul_host)
                consul_obj.agent.service.deregister(instance_id)

        else:
            logging.info("Not unregistering '%s', as it's not registered",
                         instance_id)


    def create_container(self, instance_num,
                         other_instance_num,
                         password,
                         password_base64):
        blueprint = self.blueprint
        allocation = self.allocation

        instance_id = self.group_id + '_' + instance_num
        addr = blueprint['instances'][instance_num]['addr']
        memsize = blueprint['memsize']
        network_settings = Sense.network_settings()
        network_name = network_settings['network_name']
        if not network_name:
            raise RuntimeError("Network name is not specified in settings")

        docker_host = allocation['instances'][instance_num]['host']
        docker_hosts = Sense.docker_hosts()

        docker_addr = None
        for host in docker_hosts:
            if host['addr'].split(':')[0] == docker_host or \
               host['consul_host'] == docker_host:
                docker_addr = host['addr']

        if not docker_addr:
            raise RuntimeError("No such Docker host: '%s'" % docker_host)

        replica_ip = None
        if other_instance_num is not None:
            replica_ip = blueprint['instances'][other_instance_num]['addr']

        docker_obj = docker.APIClient(base_url=docker_addr,
                                   tls=global_env.docker_tls_config)

        self.ensure_image(docker_addr)
        self.ensure_network(docker_addr)

        if not replica_ip:
            logging.info("Creating memcached '%s' on '%s' with ip '%s'",
                         instance_id, docker_obj.base_url, addr)
        else:
            logging.info("Creating memcached '%s' on '%s' with ip '%s'" +
                         " and replication source: '%s'",
                         instance_id, docker_obj.base_url, addr, replica_ip)

        ports = [(11211,'tcp')]
        port_bindings = {'11211/tcp': None}

        host_config = docker_obj.create_host_config(
            restart_policy =
            {
                "MaximumRetryCount": 0,
                "Name": "unless-stopped"
            },
            port_bindings = port_bindings
        )

        cmd = 'tarantool /opt/tarantool/app.lua'

        networking_config = {
            'EndpointsConfig':
            {
                network_name:
                {
                    'IPAMConfig':
                    {
                        "IPv4Address": addr,
                        "IPv6Address": ""
                    },
                    "Links": [],
                    "Aliases": []
                }
            }
        }

        environment = {}

        environment['TARANTOOL_SLAB_ALLOC_ARENA'] = float(memsize)/1024

        short_subnet = network_settings['subnet'].split('/')[1]
        environment['WEAVE_CIDR'] = addr + '/' + short_subnet
        environment['SERVICE_NAME'] = self.group_id
        if password:
            environment['MEMCACHED_PASSWORD'] = password
        if password_base64:
            environment['MEMCACHED_PASSWORD_BASE64'] = password_base64

        if replica_ip:
            environment['TARANTOOL_REPLICATION_SOURCE'] = replica_ip + ':3301'

        container = docker_obj.create_container(image='tarantool-cloud-memcached',
                                                name=instance_id,
                                                command=cmd,
                                                host_config=host_config,
                                                ports=ports,
#                                                networking_config=networking_config,
                                                environment=environment,
                                                labels=['tarantool'])

#        docker_obj.connect_container_to_network(container.get('Id'),
#                                                network_name,
#                                                ipv4_address=addr)
        docker_obj.start(container=container.get('Id'))

    def upgrade_container(self, instance_num):
        group_id = self.group_id

        logging.info("Upgrading container '%s'", group_id)

        blueprint = self.blueprint
        allocation = self.allocation

        instance_id = self.group_id + '_' + instance_num
        addr = blueprint['instances'][instance_num]['addr']
        memsize = blueprint['memsize']
        network_settings = Sense.network_settings()
        network_name = network_settings['network_name']
        if not network_name:
            raise RuntimeError("Network name is not specified in settings")

        docker_host = allocation['instances'][instance_num]['host']
        docker_hosts = Sense.docker_hosts()

        docker_addr = None
        for host in docker_hosts:
            if host['addr'].split(':')[0] == docker_host or \
               host['consul_host'] == docker_host:
                docker_addr = host['addr']

        if not docker_addr:
            raise RuntimeError("No such Docker host: '%s'" % docker_host)

        replica_ip = None
        if instance_num == '2':
            replica_ip = blueprint['instances']['1']['addr']

        docker_obj = docker.APIClient(base_url=docker_addr,
                                   tls=global_env.docker_tls_config)

        self.ensure_image(docker_addr)
        self.ensure_network(docker_addr)

        mounts = docker_obj.inspect_container(instance_id)["Mounts"]
        binds = []
        for mount in mounts:
            if mount['Destination'] == '/opt/tarantool':
                # code should be upgraded along with container
                continue

            logging.info("Keeping mount %s:%s",
                         mount["Source"], mount["Destination"])
            rw_flag = "rw" if mount['RW'] else "ro"
            binds.append("%s:%s:%s" % (mount['Source'],
                                       mount['Destination'],
                                       rw_flag))

        docker_obj.stop(container=instance_id)
        docker_obj.remove_container(container=instance_id)

        ports = [(11211,'tcp')]
        port_bindings = {'11211/tcp': None}
        host_config = docker_obj.create_host_config(
            restart_policy =
            {
                "MaximumRetryCount": 0,
                "Name": "unless-stopped"
            },
            binds = binds,
            port_bindings = port_bindings
        )

        cmd = 'tarantool /opt/tarantool/app.lua'

        networking_config = {
            'EndpointsConfig':
            {
                network_name:
                {
                    'IPAMConfig':
                    {
                        "IPv4Address": addr,
                        "IPv6Address": ""
                    },
                    "Links": [],
                    "Aliases": []
                }
            }
        }

        environment = {}

        environment['TARANTOOL_SLAB_ALLOC_ARENA'] = float(memsize)/1024

        short_subnet = network_settings['subnet'].split('/')[1]
        environment['WEAVE_CIDR'] = addr + '/' + short_subnet
        environment['SERVICE_NAME'] = self.group_id
        if replica_ip:
            environment['TARANTOOL_REPLICATION_SOURCE'] = replica_ip + ':3301'

        container = docker_obj.create_container(image='tarantool-cloud-memcached',
                                                name=instance_id,
                                                command=cmd,
                                                host_config=host_config,
                                                ports=ports,
#                                                networking_config=networking_config,
                                                environment=environment,
                                                labels=['tarantool'])

#        docker_obj.connect_container_to_network(container.get('Id'),
#                                                network_name,
#                                                ipv4_address=addr)
        docker_obj.start(container=container.get('Id'))

    def remove_container(self, instance_num):
        containers = self.containers

        if instance_num not in containers['instances']:
            return

        instance_id = self.group_id + '_' + instance_num
        docker_hosts = [h['addr'].split(':')[0] for h in Sense.docker_hosts()
                        if h['status'] == 'passing']

        if containers:
            docker_host = containers['instances'][instance_num]['host']
            docker_hosts = Sense.docker_hosts()

            docker_addr = None
            for host in docker_hosts:
                if host['addr'].split(':')[0] == docker_host or \
                   host['consul_host'] == docker_host:
                    docker_addr = host['addr']
            if not docker_addr:
                raise RuntimeError("No such Docker host: '%s'" % docker_host)

            logging.info("Removing container '%s' from '%s'",
                         instance_id,
                         docker_host)

            docker_obj = docker.APIClient(base_url=docker_addr,
                                       tls=global_env.docker_tls_config)
            docker_obj.stop(container=instance_id)
            docker_obj.remove_container(container=instance_id)
        else:
            logging.info("Not removing container '%s', as it doesn't exist",
                         instance_id)

    def resize_instance(self, instance_num, memsize):
        containers = self.containers

        if instance_num not in containers['instances']:
            return

        instance_id = self.group_id + '_' + instance_num
        docker_hosts = [h['addr'].split(':')[0] for h in Sense.docker_hosts()
                        if h['status'] == 'passing']

        if containers:
            docker_host = containers['instances'][instance_num]['host']
            docker_hosts = Sense.docker_hosts()

            docker_addr = None
            for host in docker_hosts:
                if host['addr'].split(':')[0] == docker_host or \
                   host['consul_host'] == docker_host:
                    docker_addr = host['addr']
            if not docker_addr:
                raise RuntimeError("No such Docker host: '%s'" % docker_host)

            logging.info("Resizing container '%s' to %d MiB on '%s'",
                         instance_id,
                         memsize,
                         docker_host)

            docker_obj = docker.APIClient(base_url=docker_addr,
                                       tls=global_env.docker_tls_config)

            cmd = "tarantool_set_config.lua TARANTOOL_SLAB_ALLOC_ARENA " + \
                  str(float(memsize)/1024)

            exec_id = docker_obj.exec_create(self.group_id + '_' + instance_num,
                                             cmd)
            docker_obj.exec_start(exec_id)
            ret = docker_obj.exec_inspect(exec_id)

            if ret['ExitCode'] != 0:
                raise RuntimeError("Failed to set memory size for container " +
                                   instance_id)

            docker_obj.restart(container=instance_id)
        else:
            logging.info("Not resizing container '%s', as it doesn't exist",
                         instance_id)

    def set_instance_password(self, instance_num, password):
        containers = self.containers

        if instance_num not in containers['instances']:
            return

        instance_id = self.group_id + '_' + instance_num
        docker_hosts = [h['addr'].split(':')[0] for h in Sense.docker_hosts()
                        if h['status'] == 'passing']

        if containers:
            docker_host = containers['instances'][instance_num]['host']
            docker_hosts = Sense.docker_hosts()

            docker_addr = None
            for host in docker_hosts:
                if host['addr'].split(':')[0] == docker_host or \
                   host['consul_host'] == docker_host:
                    docker_addr = host['addr']
            if not docker_addr:
                raise RuntimeError("No such Docker host: '%s'" % docker_host)

            logging.info("Setting password for '%s' on '%s'",
                         instance_id,
                         docker_host)

            docker_obj = docker.APIClient(base_url=docker_addr,
                                       tls=global_env.docker_tls_config)

            cmd = "memcached_set_password.lua " + password

            exec_id = docker_obj.exec_create(self.group_id + '_' +
                                             instance_num, cmd)
            docker_obj.exec_start(exec_id)
            ret = docker_obj.exec_inspect(exec_id)

            if ret['ExitCode'] != 0:
                raise RuntimeError("Failed to set password for container " +
                                   instance_id)

        else:
            logging.info("Not setting password for '%s', as it doesn't exist",
                         instance_id)

    def get_instance_password(self, instance_num):
        containers = self.containers

        if instance_num not in containers['instances']:
            return

        instance_id = self.group_id + '_' + instance_num
        docker_hosts = [h['addr'].split(':')[0] for h in Sense.docker_hosts()
                        if h['status'] == 'passing']

        if containers:
            docker_host = containers['instances'][instance_num]['host']
            docker_hosts = Sense.docker_hosts()

            docker_addr = None
            for host in docker_hosts:
                if host['addr'].split(':')[0] == docker_host or \
                   host['consul_host'] == docker_host:
                    docker_addr = host['addr']
            if not docker_addr:
                raise RuntimeError("No such Docker host: '%s'" % docker_host)

            logging.info("Getting password for '%s' on '%s'",
                         instance_id,
                         docker_host)

            docker_obj = docker.APIClient(base_url=docker_addr,
                                       tls=global_env.docker_tls_config)

            try:
                strm, stat = docker_obj.get_archive(instance_id, '/opt/tarantool/auth.sasldb')
                bio = io.BytesIO()
                shutil.copyfileobj(strm, bio)
                bio.seek(0)
                tar = tarfile.open(fileobj=bio)

                fobj = tar.extractfile('auth.sasldb')
                return base64.b64encode(gzip.compress(fobj.read()))
            except docker.errors.NotFound:
                return None

        else:
            raise RuntimeError("No such container: %s", instance_id)


    @classmethod
    def ensure_image(cls, docker_addr, force=False):
        docker_obj = docker.APIClient(base_url=docker_addr,
                                   tls=global_env.docker_tls_config)
        image_exists = any(['tarantool-cloud-memcached:latest' in (i['RepoTags'] or [])
                            for i in docker_obj.images()])

        if image_exists and not force:
            return

        docker_obj.pull("tarantool/tarantool")

        dockerfile_path = os.path.dirname(os.path.realpath(__file__))
        dockerfile_path = os.path.join(dockerfile_path,
                                       'docker/tarantool-cloud-memcached')

        response = docker_obj.build(path=dockerfile_path,
                                    rm=True,
                                    tag='tarantool-cloud-memcached',
                                    dockerfile='Dockerfile')

        for line in response:
            for line in line.decode('utf-8').split('\r\n'):
                if not line:
                    continue
                decoded_line = json.loads(line)
                if 'stream' in decoded_line:
                    logging.info("Build tarantool on %s: %s",
                                 docker_addr,
                                 decoded_line['stream'])

    def ensure_network(self, docker_addr):
        docker_obj = docker.APIClient(base_url=docker_addr,
                                   tls=global_env.docker_tls_config)

        settings = Sense.network_settings()
        network_name = settings['network_name']
        subnet = settings['subnet']

        if not network_name:
            raise RuntimeError("Network name not specified")

        network_exists = any([n['Name'] == network_name
                              for n in docker_obj.networks()])

        if network_exists:
            return

        if not settings['create_automatically']:
            raise RuntimeError(("No network '%s' exists and automatic creation" +
                                "prohibited") % network_name)

        ipam_pool = docker.utils.create_ipam_pool(
            subnet=subnet
        )
        ipam_config = docker.utils.create_ipam_config(
            pool_configs=[ipam_pool]
        )

        logging.info("Creating network '%s'", network_name)
        docker_obj.create_network(name=network_name,
                                  driver='bridge',
                                  ipam=ipam_config)
