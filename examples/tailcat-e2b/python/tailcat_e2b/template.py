from e2b import Template

TEMPLATE_ALIAS = "tailcat"
TAILCAT_VERSION = "0.4.0"
TAILCAT_URL = (
    "https://github.com/tailscale/tailcat/releases/download/"
    f"v{TAILCAT_VERSION}/tailcat_{TAILCAT_VERSION}_linux_amd64.tar.gz"
)

# The E2B sandbox's only IPv4 address is link-local (169.254.x.x). Tailscale's
# interface scan treats that as "no IPv4", skips STUN, and never learns the
# sandbox's public endpoint, so every connection stays on the DERP relay.
# Adding any routable address to eth0 is enough to turn on NAT traversal.
# See ../../README.md, "Quirks you need to know about".
ENABLE_DIRECT_PATHS_CMD = "sudo ip addr add 10.200.0.1/32 dev eth0 2>/dev/null || true"

tailcat_template = (
    Template()
    .from_image("ubuntu:24.04")
    .set_envs({"DEBIAN_FRONTEND": "noninteractive"})
    # openssh-client: `tailcat cp` and `tailcat ssh` drive the system scp/ssh.
    # socat: turns a tailcat stdio pipe into a local TCP port (sandbox-tests-local-dev-server).
    # iproute2: the `ip` command for the dummy address above.
    .apt_install(["curl", "ca-certificates", "openssh-client", "socat", "iproute2", "python3"])
    .run_cmd(
        f"curl -fsSL -o /tmp/tailcat.tgz {TAILCAT_URL}"
        " && mkdir -p /tmp/tc && tar xzf /tmp/tailcat.tgz -C /tmp/tc"
        " && install -m 0755 $(find /tmp/tc -type f -name tailcat) /usr/local/bin/tailcat"
        " && rm -rf /tmp/tc /tmp/tailcat.tgz && tailcat version",
        user="root",
    )
    .set_user("user")
    .set_workdir("/home/user")
    .set_start_cmd(ENABLE_DIRECT_PATHS_CMD, ready_cmd="tailcat version")
)
