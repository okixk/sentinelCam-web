FROM httpd:2.4-alpine

RUN apk add --no-cache openssl bash \
    && mkdir -p /etc/apache/certs

# Enable the modules we need (ssl, rewrite, proxy, headers, wstunnel) and
# include both the per-host Define file (written by the entrypoint) and the
# vhost config. mod_proxy_wstunnel is needed for the WebSocket ingest channels
# from the Pi and from the worker.
RUN sed -i \
    -e 's|^#\(LoadModule ssl_module .*\)|\1|' \
    -e 's|^#\(LoadModule socache_shmcb_module .*\)|\1|' \
    -e 's|^#\(LoadModule rewrite_module .*\)|\1|' \
    -e 's|^#\(LoadModule proxy_module .*\)|\1|' \
    -e 's|^#\(LoadModule proxy_http_module .*\)|\1|' \
    -e 's|^#\(LoadModule proxy_wstunnel_module .*\)|\1|' \
    -e 's|^#\(LoadModule headers_module .*\)|\1|' \
    /usr/local/apache2/conf/httpd.conf \
    && printf '\nInclude conf/extra/sc-define.conf\nInclude conf/extra/httpd-vhosts.conf\n' \
       >> /usr/local/apache2/conf/httpd.conf

COPY httpd-vhosts.conf /usr/local/apache2/conf/extra/httpd-vhosts.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && printf 'Define SC_PUBLIC_HOSTNAME "sentinelcam.local"\n' \
       > /usr/local/apache2/conf/extra/sc-define.conf

EXPOSE 80 443

ENTRYPOINT ["/entrypoint.sh"]
CMD ["httpd-foreground"]
