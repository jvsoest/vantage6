"""
Node policies on which algorithms are allowed to run on the node.
"""

import logging
import re

from vantage6.common import logger_name
from vantage6.common.client.node_client import NodeClient
from vantage6.common.docker.addons import get_image_name_wo_tag
from vantage6.common.globals import BASIC_PROCESSING_IMAGE, NodePolicy


class ImagePolicyMixin:
    """
    Evaluates the node policies on allowed algorithms.

    Classes using this mixin should set the attributes ``client`` (a
    :class:`NodeClient`) and ``_policies`` (as returned by
    :meth:`_setup_policies`), and implement :meth:`_resolve_digest`.
    """

    log = logging.getLogger(logger_name(__name__))

    client: NodeClient
    _policies: dict

    def _resolve_digest(self, image: str) -> str | None:
        """
        Get the digest of a docker image.

        Parameters
        ----------
        image: str
            Name of the image

        Returns
        -------
        str | None
            Digest of the image, or None if it could not be obtained
        """
        raise NotImplementedError

    def _setup_policies(self, config: dict) -> dict:
        """
        Set up policies for the node.

        Parameters
        ----------
        config: dict
            Configuration dictionary

        Returns
        -------
        dict
            Dictionary with the policies
        """
        policies = config.get("policies", {})
        if not policies or (
            not policies.get(NodePolicy.ALLOWED_ALGORITHMS)
            and not policies.get(NodePolicy.ALLOWED_ALGORITHM_STORES)
        ):
            self.log.warning(
                "No policies on allowed algorithms have been set for this node!"
            )
            self.log.warning(
                "This means that all algorithms are allowed to run on this node."
            )
        return policies

    def is_docker_image_allowed(self, evaluated_img: str, task_info: dict) -> bool:
        """
        Checks the docker image name.

        Against a list of regular expressions as defined in the configuration
        file. If no expressions are defined, all docker images are accepted.

        Parameters
        ----------
        assessed_img: str
            URI of the docker image of which we are checking if it is allowed
        task_info: dict
            Dictionary with information about the task

        Returns
        -------
        bool
            Whether docker image is allowed or not
        """
        # check if algorithm matches any of the regex cases
        allow_basics = self._policies.get(NodePolicy.ALLOW_BASICS_ALGORITHM, True)
        allowed_algorithms = self._policies.get(NodePolicy.ALLOWED_ALGORITHMS)
        allowed_stores = self._policies.get(NodePolicy.ALLOWED_ALGORITHM_STORES)
        allow_either_whitelist_or_store = self._policies.get(
            "allow_either_whitelist_or_store", False
        )
        if evaluated_img.startswith(BASIC_PROCESSING_IMAGE):
            if not allow_basics:
                self.log.warning(
                    "A task was sent with a basics algorithm that "
                    "this node does not allow to run."
                )
                return False
            # else: basics are allowed, so we don't need to check the regex

        # check if user or their organization is allowed
        allowed_users = self._policies.get(NodePolicy.ALLOWED_USERS, [])
        allowed_orgs = self._policies.get(NodePolicy.ALLOWED_ORGANIZATIONS, [])
        if allowed_users or allowed_orgs:
            is_allowed = self.client.check_user_allowed_to_send_task(
                allowed_users,
                allowed_orgs,
                task_info["init_org"]["id"],
                task_info["init_user"]["id"],
            )
            if not is_allowed:
                self.log.warning(
                    "A task was sent by a user or organization that this node does not "
                    "allow to start tasks."
                )
                return False

        algorithm_whitelisted = False
        if allowed_algorithms:
            if isinstance(allowed_algorithms, str):
                allowed_algorithms = [allowed_algorithms]
            try:
                evaluated_img_wo_tag = get_image_name_wo_tag(evaluated_img)
            except Exception as exc:
                self.log.warning(
                    "Could not parse image with name %s: %s",
                    evaluated_img,
                    exc,
                )
                evaluated_img_wo_tag = None
            for allowed_algo in allowed_algorithms:
                if not self._is_regex_pattern(allowed_algo):
                    try:
                        allowed_wo_tag = get_image_name_wo_tag(allowed_algo)
                    except Exception as exc:
                        self.log.warning(
                            "Could not parse allowed_algorithm policy with name %s: %s",
                            allowed_algo,
                            exc,
                        )
                        self.log.warning("Skipping policy as it cannot be parsed")
                        continue  # skip policy as it cannot be parsed
                    if allowed_algo == evaluated_img:
                        # OK if allowed algorithm and provided algorithm match exactly
                        algorithm_whitelisted = True
                        break
                    elif allowed_algo == evaluated_img_wo_tag:
                        # OK if allowed algorithm is an image name without a tag, and
                        # the provided image is the same but includes extra tag
                        algorithm_whitelisted = True
                        break
                    elif allowed_wo_tag == evaluated_img_wo_tag:
                        # The allowed image and the evaluated image are indeed the same
                        # image but the allowed image only allows certain tags or sha's.
                        # Gather the digests of the images and compare them - if they
                        # are the same, the image is allowed.
                        # Note that by comparing the digests, we also take into account
                        # the situation where e.g. the allowed image has a tag, but the
                        # evaluated image has a sha256.
                        digest_evaluated_image = self._resolve_digest(evaluated_img)
                        if not digest_evaluated_image:
                            self.log.warning(
                                "Could not obtain digest for image %s",
                                evaluated_img,
                            )
                        digest_policy_image = self._resolve_digest(allowed_algo)
                        if not digest_policy_image:
                            self.log.warning(
                                "Could not obtain digest for image %s", allowed_algo
                            )
                        if (
                            digest_evaluated_image
                            and digest_policy_image
                            and (digest_evaluated_image == digest_policy_image)
                        ):
                            algorithm_whitelisted = True
                            break
                else:
                    expr_ = re.compile(allowed_algo)
                    if expr_.match(evaluated_img):
                        algorithm_whitelisted = True

        store_whitelisted = False
        if allowed_stores:
            # get the store from the task_info
            try:
                store_id = task_info["algorithm_store"]["id"]
            except Exception:
                store_id = None
            if store_id:
                store_info = self.client.algorithm_store.get(store_id)
                store_from_task = store_info["url"]
                # check if the store matches any of the regex cases
                if isinstance(allowed_stores, str):
                    allowed_stores = [allowed_stores]
                store_url_whitelisted = False
                for allowed_store in allowed_stores:
                    if not self._is_regex_pattern(allowed_store):
                        # check if string matches exactly
                        if allowed_store == store_from_task:
                            store_url_whitelisted = True
                    else:
                        expr_ = re.compile(allowed_store)
                        if expr_.match(store_from_task):
                            store_url_whitelisted = True

                if store_url_whitelisted:
                    # The store's URL is whitelisted, but that alone does not prove
                    # that the evaluated image is actually a registered, approved
                    # algorithm in that store - the server is the one asserting that
                    # association, and the server is not fully trusted here.
                    # Re-verify directly with the store itself instead of trusting
                    # the server's claim.
                    # `store_info['url']` is the full base URL the store was
                    # registered with, including its API path (default `/api`, but
                    # configurable per store) - nothing needs to be appended here.
                    self.client.algorithm_store.url = store_info["url"]
                    self.client.algorithm_store.store_id = store_id
                    algorithm = self.client.algorithm_store.get_algorithm(evaluated_img)
                    if algorithm:
                        store_whitelisted = True
                    else:
                        self.log.warning(
                            "Algorithm store %s did not confirm that image %s is a "
                            "registered, approved algorithm there. Denying the "
                            "allowed_algorithm_stores policy for this image. This "
                            "can also happen if the algorithm store does not yet "
                            "support node verification requests.",
                            store_from_task,
                            evaluated_img,
                        )

        allowed_from_whitelist = not allowed_algorithms or algorithm_whitelisted
        allowed_from_store = not allowed_stores or store_whitelisted
        if allow_either_whitelist_or_store:
            # if we allow an algorithm if it is defined in the whitelist or the store,
            # we return True if either the algorithm or the store is whitelisted
            allowed = allowed_from_whitelist or allowed_from_store
        else:
            # only allow algorithm if it is allowed for both the allowed_algorithms and
            # the allowed_algorithm_stores
            allowed = allowed_from_whitelist and allowed_from_store

        if not allowed:
            self.log.warning(
                "This node does not allow the algorithm %s to run!", evaluated_img
            )

        return allowed

    @staticmethod
    def _is_regex_pattern(pattern: str) -> bool:
        """
        Check if a string just a string or if it is a regex pattern. Note that there is
        no failsafe way to do this so we make a best effort.

        Note, for instance, that if a user provides the allowed algorithm "some.name",
        we will interpret this as a regular string. This prevents that "someXname" is
        allowed as well. The user is thus not able to define a regex pattern with only
        a dot as special character. However we expect that this use case is extremely
        rare - not doing so is likely to lead to regex's that lead to unintended
        algorithms passing the filter criteria.

        Parameters
        ----------
        pattern: str
            String to be checked

        Returns
        -------
        bool
            Returns False if the pattern is a normal string, True if it is a regex.
        """
        # Inspired by
        # https://github.com/corydolphin/flask-cors/blob/main/flask_cors/core.py#L254.
        common_regex_chars = [
            "*",
            "\\",
            "?",
            "$",
            "^",
            "[",
            "]",
            "(",
            ")",
            "{",
            "}",
            "|",
            "+",
            r"\.",
        ]
        # Use common characters used in regular expressions as a proxy
        # for if this string is in fact a regex.
        return any(c in pattern for c in common_regex_chars)
