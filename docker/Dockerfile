FROM alpine:3.15

# Install Java via the package manager
RUN apk update 																		&& \
	apk upgrade																		&& \
	apk add --no-cache openjdk8-jre

# Install Python, PIP & WhiteSource
RUN apk add --no-cache python3 														&& \
	python3 -m ensurepip															&& \
	pip3 install --upgrade pip setuptools											&& \
	rm -r /usr/lib/python*/ensurepip												&& \
	if [ ! -e /usr/bin/pip ]; then ln -s pip3 /usr/bin/pip ; fi 					&& \
	if [[ ! -e /usr/bin/python ]]; then ln -sf /usr/bin/python3 /usr/bin/python; fi && \
	pip install --trusted-host pypi.python.org ws-nexus-integration==0.2.10a1		&& \
	mkdir -p /opt/whitesource														&& \
	rm -r /root/.cache

# Install WhiteSource
# RUN pip install --trusted-host pypi.python.org ws-nexus-integration==0.2.10a1

# Set JAVA_HOME environment variable; uncomment the line below if you need it
# ENV JAVA_HOME="/usr/lib/jvm/java-1.8-openjdk"

WORKDIR /opt/whitesource

CMD ["python", "/usr/bin/ws_nexus_integration"]
