ARG BUILD_FROM
FROM ${BUILD_FROM}

# adb talks to the Portal over the LAN (192.168.4.71:5555); python3 runs the
# bridge daemon; websocket-client is its only non-stdlib dependency.
RUN apk add --no-cache \
    python3 \
    py3-pip \
    android-tools \
    && pip3 install --break-system-packages websocket-client

COPY portal_chatgpt_bridge.py /opt/bridge/portal_chatgpt_bridge.py
COPY portal_chatgpt_kiosk.js /opt/bridge/portal_chatgpt_kiosk.js
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
