FROM tarantool/tarantool

RUN mkdir /var/lib/mon.d

COPY tarantool_replication.sh /var/lib/mon.d
RUN chmod 755 /var/lib/mon.d/tarantool_replication.sh
COPY tarantool_memory.sh /var/lib/mon.d
RUN chmod 755 /var/lib/mon.d/tarantool_memory.sh
COPY tarantool-memcached.conf /etc/sasl2/tarantool-memcached.conf

COPY app.lua /opt/tarantool
COPY memcached_set_password.lua /usr/local/bin/

EXPOSE 11211

CMD ["tarantool", "/opt/tarantool/app.lua"]
