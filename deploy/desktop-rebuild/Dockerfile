ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root
RUN install -d -o wechat -g wechat -m 700 /run/user/1000 \
    && chown -R wechat:wechat /opt/tools /data
COPY --chown=wechat:wechat entrypoint.sh /opt/desktop-session
COPY --chown=wechat:wechat launch-wechat.sh /opt/tools/launch-wechat
COPY --chown=wechat:wechat enter-session.py /opt/enter-session.py
RUN if [ ! -f /opt/tools/a11y-dump.real ]; then \
      cp /opt/tools/a11y-dump /opt/tools/a11y-dump.real; \
    fi
COPY --chown=wechat:wechat a11y-dump-wrapper.py /opt/tools/a11y-dump
RUN chmod 755 /opt/desktop-session /opt/tools/launch-wechat \
    /opt/tools/a11y-dump /opt/tools/a11y-dump.real

USER wechat
ENV HOME=/home/wechat \
    WECHAT_HOME=/home/wechat \
    DISPLAY=:99 \
    XDG_RUNTIME_DIR=/run/user/1000 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
    PULSE_SERVER=unix:/run/user/1000/pulse/native \
    QT_SCALE_FACTOR=1 \
    QT_AUTO_SCREEN_SCALE_FACTOR=0 \
    QT_ENABLE_HIGHDPI_SCALING=0 \
    QT_ACCESSIBILITY=1 \
    QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 \
    GTK_MODULES=gail:atk-bridge
ENTRYPOINT ["/opt/desktop-session"]
