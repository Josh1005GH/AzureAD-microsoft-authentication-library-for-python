"""This module is an adaptor to the underlying broker.
It relies on PyMsalRuntime which is the package providing broker's functionality.
"""
from threading import Event
import json
import logging
import time
import uuid


logger = logging.getLogger(__name__)
try:
    import pymsalruntime  # ImportError would be raised on unsupported platforms such as Windows 8
    # Its API description is available in site-packages/pymsalruntime/PyMsalRuntime.pyi
    pymsalruntime.register_logging_callback(lambda message, level: {  # New in pymsalruntime 0.7
        pymsalruntime.LogLevel.TRACE: logger.debug,  # Python has no TRACE level
        pymsalruntime.LogLevel.DEBUG: logger.debug,
        # Let broker's excess info, warning and error logs map into default DEBUG, for now
        #pymsalruntime.LogLevel.INFO: logger.info,
        #pymsalruntime.LogLevel.WARNING: logger.warning,
        #pymsalruntime.LogLevel.ERROR: logger.error,
        pymsalruntime.LogLevel.FATAL: logger.critical,
        }.get(level, logger.debug)(message))
except (ImportError, AttributeError):  # AttributeError happens when a prior pymsalruntime uninstallation somehow leaved an empty folder behind
    # PyMsalRuntime currently supports these Windows versions, listed in this MSFT internal link
    # https://github.com/AzureAD/microsoft-authentication-library-for-cpp/pull/2406/files
    raise ImportError(  # TODO: Remove or adjust this line right before merging this PR
        'You need to install dependency by: pip install "msal[broker]>=1.20.0b1,<2"')
# Other exceptions (possibly RuntimeError) would be raised if its initialization fails


class RedirectUriError(ValueError):
    pass


class TokenTypeError(ValueError):
    pass


class _CallbackData:
    def __init__(self):
        self.signal = Event()
        self.result = None

    def complete(self, result):
        self.signal.set()
        self.result = result


def _convert_error(error, client_id):
    context = error.get_context()  # Available since pymsalruntime 0.0.4
    if (
            "AADSTS50011" in context  # In WAM, this could happen on both interactive and silent flows
            or "AADSTS7000218" in context  # This "request body must contain ... client_secret" is just a symptom of current app has no WAM redirect_uri
            ):
        raise RedirectUriError(  # This would be seen by either the app developer or end user
            "MsalRuntime won't work unless this one more redirect_uri is registered to current app: "
            "ms-appx-web://Microsoft.AAD.BrokerPlugin/{}".format(client_id))
        # OTOH, AAD would emit other errors when other error handling branch was hit first,
        # so, the AADSTS50011/RedirectUriError is not guaranteed to happen.
    return {
        "error": "broker_error",  # Note: Broker implies your device needs to be compliant.
            # You may use "dsregcmd /status" to check your device state
            # https://docs.microsoft.com/en-us/azure/active-directory/devices/troubleshoot-device-dsregcmd
        "error_description": "{}. Status: {}, Error code: {}, Tag: {}".format(
            context,
            error.get_status(), error.get_error_code(), error.get_tag()),
        "_broker_status": error.get_status(),
        "_broker_error_code": error.get_error_code(),
        "_broker_tag": error.get_tag(),
        }


def _read_account_by_id(account_id, correlation_id):
    """Return an instance of MSALRuntimeError or MSALRuntimeAccount, or None"""
    callback_data = _CallbackData()
    pymsalruntime.read_account_by_id(
        account_id,
        correlation_id,
        lambda result, callback_data=callback_data: callback_data.complete(result)
        )
    callback_data.signal.wait()
    return (callback_data.result.get_error() or callback_data.result.get_account()
        or None)  # None happens when the account was not created by broker


def _convert_result(result, client_id, expected_token_type=None):  # Mimic an on-the-wire response from AAD
    error = result.get_error()
    if error:
        return _convert_error(error, client_id)
    id_token_claims = json.loads(result.get_id_token()) if result.get_id_token() else {}
    account = result.get_account()
    assert account, "Account is expected to be always available"
    ## Note: As of pymsalruntime 0.1.0, only wam_account_ids property is available
    #account.get_account_property("wam_account_ids")
    return_value = {k: v for k, v in {
        "access_token": result.get_access_token(),
        "expires_in": result.get_access_token_expiry_time() - int(time.time()),  # Convert epoch to count-down
        "id_token": result.get_raw_id_token(),  # New in pymsalruntime 0.8.1
        "id_token_claims": id_token_claims,
        "client_info": account.get_client_info(),
        "_account_id": account.get_account_id(),
		"token_type": expected_token_type or "Bearer",  # Workaround its absence from broker
        }.items() if v}
    likely_a_cert = return_value["access_token"].startswith("AAAA")  # Empirical observation
    if return_value["token_type"].lower() == "ssh-cert" and not likely_a_cert:
        raise TokenTypeError("Broker could not get an SSH Cert: {}...".format(
            return_value["access_token"][:8]))
    granted_scopes = result.get_granted_scopes()  # New in pymsalruntime 0.3.x
    if granted_scopes:
        return_value["scope"] = " ".join(granted_scopes)  # Mimic the on-the-wire data format
    return return_value


def _get_new_correlation_id():
    return str(uuid.uuid4())


def _enable_msa_pt(params):
    params.set_additional_parameter("msal_request_type", "consumer_passthrough")  # PyMsalRuntime 0.8+


def _signin_silently(
        authority, client_id, scopes, correlation_id=None, claims=None,
        enable_msa_pt=False,
        **kwargs):
    params = pymsalruntime.MSALRuntimeAuthParameters(client_id, authority)
    params.set_requested_scopes(scopes)
    if claims:
        params.set_decoded_claims(claims)
    callback_data = _CallbackData()
    for k, v in kwargs.items():  # This can be used to support domain_hint, max_age, etc.
        if v is not None:
            params.set_additional_parameter(k, str(v))
    if enable_msa_pt:
        _enable_msa_pt(params)
    pymsalruntime.signin_silently(
        params,
        correlation_id or _get_new_correlation_id(),
        lambda result, callback_data=callback_data: callback_data.complete(result))
    callback_data.signal.wait()
    return _convert_result(
        callback_data.result, client_id, expected_token_type=kwargs.get("token_type"))


def _signin_interactively(
        authority, client_id, scopes,
        parent_window_handle,  # None means auto-detect for console apps
        prompt=None,
        login_hint=None,
        claims=None,
        correlation_id=None,
        enable_msa_pt=False,
        **kwargs):
    params = pymsalruntime.MSALRuntimeAuthParameters(client_id, authority)
    params.set_requested_scopes(scopes)
    params.set_redirect_uri("placeholder")  # pymsalruntime 0.1 requires non-empty str,
        # the actual redirect_uri will be overridden by a value hardcoded by the broker
    if prompt:
        if prompt == "select_account":
            if login_hint:
                # FWIW, AAD's browser interactive flow would honor select_account
                # and ignore login_hint in such a case.
                # But pymsalruntime 0.3.x would pop up a meaningless account picker
                # and then force the account_hint user to re-input password. Not what we want.
                # https://identitydivision.visualstudio.com/Engineering/_workitems/edit/1744492
                login_hint = None  # Mimicing the AAD behavior
                logger.warning("Using both select_account and login_hint is ambiguous. Ignoring login_hint.")
        else:
            logger.warning("prompt=%s is not supported by this module", prompt)
    if parent_window_handle is None:
        # This fixes account picker hanging in IDE debug mode on some machines
        params.set_additional_parameter("msal_gui_thread", "true")  # Since pymsalruntime 0.8.1
    if enable_msa_pt:
        _enable_msa_pt(params)
    for k, v in kwargs.items():  # This can be used to support domain_hint, max_age, etc.
        if v is not None:
            params.set_additional_parameter(k, str(v))
    if claims:
        params.set_decoded_claims(claims)
    callback_data = _CallbackData()
    pymsalruntime.signin_interactively(
        parent_window_handle or pymsalruntime.get_console_window() or pymsalruntime.get_desktop_window(),  # Since pymsalruntime 0.2+
        params,
        correlation_id or _get_new_correlation_id(),
        login_hint,  # None value will be accepted since pymsalruntime 0.3+
        lambda result, callback_data=callback_data: callback_data.complete(result))
    callback_data.signal.wait()
    return _convert_result(
        callback_data.result, client_id, expected_token_type=kwargs.get("token_type"))


def _acquire_token_silently(
        authority, client_id, account_id, scopes, claims=None, correlation_id=None,
        **kwargs):
    # For MSA PT scenario where you use the /organizations, yes,
    # acquireTokenSilently is expected to fail.  - Sam Wilson
    correlation_id = correlation_id or _get_new_correlation_id()
    account = _read_account_by_id(account_id, correlation_id)
    if isinstance(account, pymsalruntime.MSALRuntimeError):
        return _convert_error(account, client_id)
    if account is None:
        return
    params = pymsalruntime.MSALRuntimeAuthParameters(client_id, authority)
    params.set_requested_scopes(scopes)
    if claims:
        params.set_decoded_claims(claims)
    for k, v in kwargs.items():  # This can be used to support domain_hint, max_age, etc.
        if v is not None:
            params.set_additional_parameter(k, str(v))
    callback_data = _CallbackData()
    pymsalruntime.acquire_token_silently(
        params,
        correlation_id,
        account,
        lambda result, callback_data=callback_data: callback_data.complete(result))
    callback_data.signal.wait()
    return _convert_result(
        callback_data.result, client_id, expected_token_type=kwargs.get("token_type"))


def _signout_silently(client_id, account_id, correlation_id=None):
    correlation_id = correlation_id or _get_new_correlation_id()
    account = _read_account_by_id(account_id, correlation_id)
    if isinstance(account, pymsalruntime.MSALRuntimeError):
        return _convert_error(account, client_id)
    if account is None:
        return
    callback_data = _CallbackData()
    pymsalruntime.signout_silently(  # New in PyMsalRuntime 0.7
        client_id,
        correlation_id,
        account,
        lambda result, callback_data=callback_data: callback_data.complete(result))
    callback_data.signal.wait()
    error = callback_data.result.get_error()
    if error:
        return _convert_error(error, client_id)

