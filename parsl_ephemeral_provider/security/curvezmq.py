"""CurveZMQ certificate distribution for HTEX workers (#62).

Parsl's ``HighThroughputExecutor`` encrypts the worker/interchange channel with
CurveZMQ by default. The interchange generates the certificates on the *driver*
machine, under its ``run_dir``, and passes the directory to workers as
``--cert_dir``. A worker on EC2 cannot read the driver's filesystem, so an
unmodified ``encrypted=True`` config produces workers that die with
``FileNotFoundError: .../certificates`` -- which is why the examples set
``encrypted=False`` and lean on VPC isolation instead.

This module ships the certificates to the worker out of band, so
``encrypted=True`` works for cross-VPC, cross-account, and over-the-internet
deployments where there is no shared network boundary to rely on.

What a worker actually needs
----------------------------
Verified against pyzmq/Parsl rather than assumed, because it determines whether
this can be done safely at all. ``curvezmq.ClientContext.socket`` calls
``_load_certificate`` twice -- once for ``client`` and once for ``server`` -- and
that helper reads the ``.key_secret`` file in both cases. So the worker needs:

* ``client.key_secret`` -- its own keypair, and
* ``server.key_secret`` -- from which it uses only the *public* half.

The ``.key`` files (public-only) are never read and are not shipped.

The uncomfortable consequence is that distributing certificates means shipping
the interchange's **server secret key** to every worker. That is Parsl's file
layout, not a choice available here: ``ClientContext`` needs the server's public
key and only ever reads it out of ``server.key_secret``. It is the reason this
module refuses to use anything but an encrypted transport, tags what it writes
for cleanup, and deletes the material on shutdown. Anyone holding those two
files can impersonate the interchange to a worker, so the blast radius must be
bounded in time.

``_load_certificate`` also rejects a certificate directory whose mode is not
exactly ``0700``, so the worker-side script creates it with ``mkdir -m 700``.

Transport
---------
SSM Parameter Store as a ``SecureString``. Two properties make it the right
choice over S3 or UserData:

* ``AmazonSSMManagedInstanceCore`` -- already attached to the instance profile
  this provider creates for SSM dispatch -- grants ``ssm:GetParameter`` on
  ``*``, and the ``alias/aws/ssm`` managed key's policy grants ``kms:Decrypt``
  to any principal in the account when the call arrives via SSM. So a worker can
  read a SecureString with no additional IAM or KMS configuration. Both were
  confirmed against the live account.
* UserData is the alternative and is disqualified: it is readable for the life of
  the instance by anything that can reach IMDS, including every process on the
  instance, and it is returned in plaintext by ``DescribeInstanceAttribute``.
  Secret key material does not belong there.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import base64
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from botocore.exceptions import ClientError

from ..exceptions import ProviderConfigurationError, ResourceCreationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import boto3

logger = logging.getLogger(__name__)

#: Files a worker's ``ClientContext`` actually opens. Both are secret-bearing;
#: see the module docstring for why ``server.key_secret`` is unavoidable.
WORKER_CERT_FILES = ("client.key_secret", "server.key_secret")

#: Mode ``curvezmq._load_certificate`` demands of the certificate directory.
CERT_DIR_MODE = "700"

#: SecureString values are capped at 4096 characters by Parameter Store
#: (advanced tier reaches 8192, but requires opting in and is billed per
#: parameter, so the standard tier is what this assumes).
_MAX_SECURESTRING_CHARS = 4096

#: ``--cert_dir <path>`` or ``--cert_dir=<path>``, as HTEX's DEFAULT_LAUNCH_CMD
#: interpolates it. Parsl writes the literal string "None" when encryption is
#: off, which must not be mistaken for a real path.
_CERT_DIR_RE = re.compile(r"--cert_dir[=\s]+(?P<path>\S+)")


def extract_cert_dir(command: str) -> Optional[str]:
    """Return the ``--cert_dir`` path in *command*, or None if it has none.

    The provider never asks the caller where the certificates are: by the time a
    command reaches ``submit_job`` it already names the directory. HTEX creates
    the certificates in ``start()`` (``executor.py:441``) and only then calls
    ``initialize_scaling()`` (``:472``), which interpolates ``cert_dir`` into
    ``launch_cmd``. So the path in the command string is real and the files
    exist. (#62 assumed the opposite -- that certificates were generated too
    late for ``submit()`` to see them -- and dismissed this approach for it.)

    Parameters
    ----------
    command : str
        The command the executor asked the provider to run.

    Returns
    -------
    Optional[str]
        The certificate directory, or None when the command has no
        ``--cert_dir`` at all or carries Parsl's literal ``None`` placeholder
        for "encryption disabled".
    """
    match = _CERT_DIR_RE.search(command)
    if not match:
        return None
    path = match.group("path")
    # HTEX formats cert_dir=None into the command verbatim when encrypted=False.
    if path == "None":
        return None
    return path


def read_worker_certificates(cert_dir: str) -> Dict[str, str]:
    """Read the certificate files a worker needs, base64-encoded.

    Parameters
    ----------
    cert_dir : str
        The interchange's certificate directory, as named by ``--cert_dir``.

    Returns
    -------
    Dict[str, str]
        Filename to base64-encoded content, for every name in
        :data:`WORKER_CERT_FILES`.

    Raises
    ------
    ProviderConfigurationError
        If the directory or either file is missing. This is a configuration
        problem rather than an AWS one: it means the command named a
        ``cert_dir`` that the driver did not populate.
    """
    if not os.path.isdir(cert_dir):
        raise ProviderConfigurationError(
            f"Certificate directory {cert_dir} does not exist. The command "
            "requested CurveZMQ encryption, so the driver's interchange should "
            "have created it. Set encrypted=False on the executor to run "
            "without encryption."
        )

    encoded: Dict[str, str] = {}
    for name in WORKER_CERT_FILES:
        path = os.path.join(cert_dir, name)
        try:
            with open(path, "rb") as handle:
                encoded[name] = base64.b64encode(handle.read()).decode("ascii")
        except OSError as exc:
            raise ProviderConfigurationError(
                f"Could not read CurveZMQ certificate {path}: {exc}. A worker "
                f"needs both {' and '.join(WORKER_CERT_FILES)} -- it reads the "
                "server's public key out of the secret file."
            ) from exc
    return encoded


class CurveZMQCertificateDistributor:
    """Publish CurveZMQ certificates for workers to fetch, then delete them.

    One instance per operating mode. Parameters are named for the provider and
    the job, so concurrent providers do not collide and a leaked parameter is
    traceable to the run that made it.

    Parameters
    ----------
    session : boto3.Session
        Session used to build the SSM client.
    provider_id : str
        Provider ID, used in the parameter path and the ``ProviderId`` tag.
    path_prefix : str, optional
        Parameter Store prefix, by default ``/parsl-ephemeral/certs``.
    """

    def __init__(
        self,
        session: "boto3.Session",
        provider_id: str,
        path_prefix: str = "/parsl-ephemeral/certs",
    ) -> None:
        self.session = session
        self.provider_id = provider_id
        self.path_prefix = path_prefix.rstrip("/")
        # Every parameter this distributor created, so shutdown can delete them
        # without a ListParameters sweep. Persisted by the mode's save_state so a
        # provider reconstructed from state can still clean up -- otherwise the
        # secret material outlives the run that published it.
        self._published: List[str] = []

    # ------------------------------------------------------------------
    # Naming
    # ------------------------------------------------------------------

    def parameter_name(self, job_id: str) -> str:
        """Return the parameter path holding the certificates for *job_id*."""
        return f"{self.path_prefix}/{self.provider_id}/{job_id}"

    @property
    def published_parameters(self) -> List[str]:
        """Parameter paths published and not yet deleted."""
        return list(self._published)

    def adopt_published(self, names: List[str]) -> None:
        """Adopt parameter paths recorded by a previous run.

        Called from the mode's ``load_state``. Without this a provider rebuilt
        from a state file cannot delete what the original run published.
        """
        for name in names:
            if name not in self._published:
                self._published.append(name)

    # ------------------------------------------------------------------
    # Publish / revoke
    # ------------------------------------------------------------------

    def publish(self, job_id: str, cert_dir: str) -> str:
        """Publish the certificates in *cert_dir* for *job_id* to fetch.

        Parameters
        ----------
        job_id : str
            Job the certificates are for.
        cert_dir : str
            The interchange's certificate directory.

        Returns
        -------
        str
            The parameter path the worker should read.

        Raises
        ------
        ResourceCreationError
            If the parameter could not be written.
        ProviderConfigurationError
            If the certificates are missing, or too large for a standard
            SecureString.
        """
        payload = json.dumps(read_worker_certificates(cert_dir))
        if len(payload) > _MAX_SECURESTRING_CHARS:
            raise ProviderConfigurationError(
                f"CurveZMQ certificates for job {job_id} encode to "
                f"{len(payload)} characters, over Parameter Store's "
                f"{_MAX_SECURESTRING_CHARS}-character SecureString limit. "
                "CurveZMQ keys are 40 characters each, so this means the "
                "certificate files are not the ones pyzmq generates."
            )

        name = self.parameter_name(job_id)
        ssm = self.session.client("ssm")
        try:
            # Overwrite unconditionally: the warm pool reuses an instance for
            # several jobs, and a retried submit must not fail on a parameter its
            # own earlier attempt left behind.
            ssm.put_parameter(
                Name=name,
                Value=payload,
                Type="SecureString",
                Overwrite=True,
                Description=(
                    f"CurveZMQ certificates for Parsl provider {self.provider_id}"
                ),
            )
        except ClientError as exc:
            raise ResourceCreationError(
                f"Could not publish CurveZMQ certificates to {name}: {exc}"
            ) from exc

        # Tags go in a separate call. put_parameter rejects Tags together with
        # Overwrite=True, and the tag is only for traceability, so a failure here
        # must not fail the submit -- the parameter is already usable and is
        # tracked in _published for cleanup either way.
        try:
            ssm.add_tags_to_resource(
                ResourceType="Parameter",
                ResourceId=name,
                Tags=[
                    {"Key": "ProviderId", "Value": self.provider_id},
                    {"Key": "CreatedBy", "Value": "parsl-ephemeral-provider"},
                ],
            )
        except ClientError as exc:
            logger.debug(f"Could not tag certificate parameter {name}: {exc}")

        if name not in self._published:
            self._published.append(name)
        logger.debug(f"Published CurveZMQ certificates for job {job_id} to {name}")
        return name

    def revoke(self, job_id: str) -> None:
        """Delete the certificates published for *job_id*, if any."""
        self._delete([self.parameter_name(job_id)])

    def revoke_all(self) -> None:
        """Delete every certificate parameter this distributor published.

        Called from ``cleanup_infrastructure``. Best-effort and never raises: a
        cleanup failure must not mask the caller's real error. What it cannot do
        silently is *skip* the attempt, so a failure is logged at warning with
        the parameter name.
        """
        self._delete(list(self._published))

    def _delete(self, names: List[str]) -> None:
        if not names:
            return
        ssm = self.session.client("ssm")
        for name in names:
            try:
                ssm.delete_parameter(Name=name)
                logger.debug(f"Deleted CurveZMQ certificate parameter {name}")
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code != "ParameterNotFound":
                    # Left standing, so say so loudly enough to act on: this is
                    # secret key material, not a tag.
                    logger.warning(
                        f"Could not delete CurveZMQ certificate parameter "
                        f"{name}: {exc}. It holds secret key material -- delete "
                        "it by hand."
                    )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(f"Unexpected error deleting {name}: {exc}")
            finally:
                if name in self._published:
                    self._published.remove(name)

    # ------------------------------------------------------------------
    # Worker-side fetch
    # ------------------------------------------------------------------

    def fetch_script(self, job_id: str, cert_dir: str) -> str:
        """Return shell lines that fetch the certificates onto a worker.

        Emitted into UserData ahead of the worker command. It uses the AWS CLI,
        which is preinstalled on Amazon Linux 2023 -- the same assumption
        ``DEFAULT_WORKER_INIT`` already makes.

        Parameters
        ----------
        job_id : str
            Job whose certificates to fetch.
        cert_dir : str
            The directory the worker command's ``--cert_dir`` names. Recreated
            verbatim on the worker so the command needs no rewriting.

        Returns
        -------
        str
            Shell script fragment, newline-terminated.
        """
        name = self.parameter_name(job_id)
        region = self.session.region_name
        # mkdir -m 700 rather than a later chmod: curvezmq._load_certificate
        # checks the mode and raises OSError("The certificates directory must be
        # private") on anything else, so a window at the umask default is a
        # window in which a worker can fail.
        return f"""
# --- CurveZMQ certificates (#62) ---
# Fetched from Parameter Store rather than embedded in UserData: UserData is
# readable for the life of the instance through IMDS and DescribeInstanceAttribute.
mkdir -p -m {CERT_DIR_MODE} {cert_dir}
if ! aws ssm get-parameter --name '{name}' --with-decryption \\
        --region '{region}' --query Parameter.Value --output text \\
        > /tmp/parsl_certs.json; then
    echo 'parsl-ephemeral: could not fetch CurveZMQ certificates from {name}' >&2
    exit 1
fi
python3 -c "
import base64, json, os, sys
with open('/tmp/parsl_certs.json') as handle:
    certs = json.load(handle)
for filename, encoded in certs.items():
    path = os.path.join('{cert_dir}', filename)
    with open(path, 'wb') as out:
        out.write(base64.b64decode(encoded))
    os.chmod(path, 0o600)
"
rm -f /tmp/parsl_certs.json
chmod {CERT_DIR_MODE} {cert_dir}
# --- end CurveZMQ certificates ---
"""


def certificate_iam_statements(
    provider_id: str, path_prefix: str = "/parsl-ephemeral/certs"
) -> List[Dict[str, Any]]:
    """Return least-privilege IAM statements for the worker-side fetch.

    ``AmazonSSMManagedInstanceCore`` already permits ``ssm:GetParameter`` on
    ``*``, so a worker using this provider's auto-created profile needs nothing
    added. These statements exist for callers who supply their own
    ``iam_instance_profile_arn`` and want the narrowest grant that works.

    Parameters
    ----------
    provider_id : str
        Provider ID, so the resource ARN covers only this run's parameters.
    path_prefix : str, optional
        Parameter Store prefix, matching the distributor's.

    Returns
    -------
    List[Dict[str, Any]]
        Statements for an IAM policy document.
    """
    prefix = path_prefix.strip("/")
    return [
        {
            "Sid": "ParslEphemeralReadCurveZMQCertificates",
            "Effect": "Allow",
            "Action": ["ssm:GetParameter"],
            "Resource": [f"arn:aws:ssm:*:*:parameter/{prefix}/{provider_id}/*"],
        },
        {
            # SecureString decryption goes through the SSM-managed key. Scoped
            # by ViaService so the grant cannot be used to decrypt anything else.
            "Sid": "ParslEphemeralDecryptCurveZMQCertificates",
            "Effect": "Allow",
            "Action": ["kms:Decrypt"],
            "Resource": ["*"],
            "Condition": {"StringLike": {"kms:ViaService": "ssm.*.amazonaws.com"}},
        },
    ]
