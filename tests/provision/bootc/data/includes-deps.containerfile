FROM quay.io/fedora/fedora-bootc:latest

RUN dnf -y install cloud-init rsync vim && \
    ln -s ../cloud-init.target /usr/lib/systemd/system/default.target.wants && \
    rm /usr/local -rf && ln -sr /var/usrlocal /usr/local && mkdir -p /var/usrlocal/bin && \
    dnf clean all
