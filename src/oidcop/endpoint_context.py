import logging
from typing import Any
from typing import Optional

from cryptojwt import KeyJar
from cryptojwt.utils import as_bytes
from jinja2 import Environment
from jinja2 import FileSystemLoader
from oidcmsg.context import OidcContext
import requests

from oidcop import rndstr
from oidcop.scopes import SCOPE2CLAIMS
from oidcop.scopes import Scopes
from oidcop.session.claims import STANDARD_CLAIMS
from oidcop.session.manager import SessionManager
from oidcop.template_handler import Jinja2TemplateHandler
from oidcop.util import get_http_params
from oidcop.util import importer

logger = logging.getLogger(__name__)


def add_path(url: str, path: str) -> str:
    if url.endswith("/"):
        if path.startswith("/"):
            return "{}{}".format(url, path[1:])

        return "{}{}".format(url, path)

    if path.startswith("/"):
        return "{}{}".format(url, path)

    return "{}/{}".format(url, path)


def init_user_info(conf, cwd: str):
    kwargs = conf.get("kwargs", {})

    if isinstance(conf["class"], str):
        return importer(conf["class"])(**kwargs)

    return conf["class"](**kwargs)


def init_service(conf, server_get=None):
    kwargs = conf.get("kwargs", {})

    kwargs["server_get"] = server_get

    if isinstance(conf["class"], str):
        return importer(conf["class"])(**kwargs)

    return conf["class"](**kwargs)


def get_token_handler_args(conf: dict) -> dict:
    """

    :param conf: The configuration
    :rtype: dict
    """
    th_args = conf.get("token_handler_args", None)
    if not th_args:
        # create 3 keys
        keydef = [
            {"type": "oct", "bytes": "24", "use": ["enc"], "kid": "code"},
            {"type": "oct", "bytes": "24", "use": ["enc"], "kid": "token"},
            {"type": "oct", "bytes": "24", "use": ["enc"], "kid": "refresh"},
        ]

        jwks_def = {
            "private_path": "private/token_jwks.json",
            "key_defs": keydef,
            "read_only": False,
        }
        th_args = {"jwks_def": jwks_def}
        for typ, tid in [("code", 600), ("token", 3600), ("refresh", 86400)]:
            th_args[typ] = {"lifetime": tid}

    return th_args


class EndpointContext(OidcContext):
    parameter = {
        "args": {},
        # "authn_broker": AuthnBroker,
        # "authz": AuthzHandling,
        "cdb": {},
        "conf": {},
        # "cookie_dealer": None,
        "cwd": "",
        "endpoint_to_authn_method": {},
        "httpc_params": {},
        # "idtoken": IDToken,
        "issuer": "",
        "jti_db": {},
        "jwks_uri": "",
        "login_hint_lookup": None,
        "login_hint2acrs": {},
        "par_db": {},
        "provider_info": {},
        "registration_access_token": {},
        "scope2claims": {},
        "seed": "",
        # "session_db": {},
        "session_manager": SessionManager,
        "sso_ttl": None,
        "symkey": "",
        "token_args_methods": [],
        # "userinfo": UserInfo,
    }

    def __init__(
            self,
            conf: dict,
            keyjar: Optional[KeyJar] = None,
            cwd: Optional[str] = "",
            cookie_dealer: Optional[Any] = None,
            httpc: Optional[Any] = None,
    ):
        OidcContext.__init__(self, conf, keyjar, entity_id=conf.get("issuer", ""))
        self.conf = conf

        # For my Dev environment
        self.cdb = {}
        self.jti_db = {}
        self.registration_access_token = {}
        # self.session_db = {}

        self.cwd = cwd

        # Those that use seed wants bytes but I can only store str.
        try:
            self.seed = as_bytes(conf["seed"])
        except KeyError:
            self.seed = as_bytes(rndstr(32))

        # Default values, to be changed below depending on configuration
        # arguments for endpoints add-ons
        self.args = {}
        self.authn_broker = None
        self.authz = None
        self.cookie_dealer = cookie_dealer
        self.endpoint_to_authn_method = {}
        self.httpc = httpc or requests
        self.idtoken = None
        self.issuer = ""
        self.jwks_uri = None
        self.login_hint_lookup = None
        self.login_hint2acrs = None
        self.par_db = {}
        self.provider_info = {}
        self.scope2claims = SCOPE2CLAIMS
        self.session_manager = None
        self.sso_ttl = 14400  # 4h
        self.symkey = rndstr(24)
        self.template_handler = None
        self.token_args_methods = []
        self.userinfo = None

        for param in [
            "issuer",
            "sso_ttl",
            "symkey",
            "client_authn",
            # "id_token_schema",
        ]:
            try:
                setattr(self, param, conf[param])
            except KeyError:
                pass

        self.th_args = get_token_handler_args(conf)

        # session db
        self._sub_func = {}
        self.do_sub_func()

        if "cookie_name" in conf:
            self.cookie_name = conf["cookie_name"]
        else:
            self.cookie_name = {
                "session": "oidcop",
                "register": "oidc_op_rp",
                "session_management": "sman",
            }

        _handler = conf.get("template_handler")
        if _handler:
            self.template_handler = _handler
        else:
            _loader = conf.get("template_loader")

            if _loader is None:
                _template_dir = conf.get("template_dir")
                if _template_dir:
                    _loader = Environment(loader=FileSystemLoader(_template_dir), autoescape=True)

            if _loader:
                self.template_handler = Jinja2TemplateHandler(_loader)

        # self.setup = {}
        jwks_uri_path = conf["keys"]["uri_path"]

        try:
            if self.issuer.endswith("/"):
                self.jwks_uri = "{}{}".format(self.issuer, jwks_uri_path)
            else:
                self.jwks_uri = "{}/{}".format(self.issuer, jwks_uri_path)
        except KeyError:
            pass

        for item in [
            "cookie_dealer",
            "authentication",
            "id_token",
            "scope2claims",
        ]:
            _func = getattr(self, "do_{}".format(item), None)
            if _func:
                _func()

        for item in ["userinfo", "login_hint_lookup", "login_hint2acrs"]:
            _func = getattr(self, "do_{}".format(item), None)
            if _func:
                _func()

        # which signing/encryption algorithms to use in what context
        self.jwx_def = {}

        # The HTTP clients request arguments
        _cnf = conf.get("httpc_params")
        if _cnf:
            self.httpc_params = get_http_params(_cnf)
        else:  # Backward compatibility
            self.httpc_params = {"verify": conf.get("verify_ssl")}

        self.set_scopes_handler()
        self.dev_auth_db = None
        self.claims_interface = None

    def set_scopes_handler(self):
        _spec = self.conf.get("scopes_handler")
        if _spec:
            _kwargs = _spec.get("kwargs", {})
            _cls = importer(_spec["class"])(**_kwargs)
            self.scopes_handler = _cls(_kwargs)
        else:
            self.scopes_handler = Scopes()

    def do_add_on(self, endpoints):
        if self.conf.get("add_on"):
            for spec in self.conf["add_on"].values():
                if isinstance(spec["function"], str):
                    _func = importer(spec["function"])
                else:
                    _func = spec["function"]
                _func(endpoints, **spec["kwargs"])

    def do_login_hint2acrs(self):
        _conf = self.conf.get("login_hint2acrs")

        if _conf:
            self.login_hint2acrs = init_service(_conf)
        else:
            self.login_hint2acrs = None

    def do_login_hint_lookup(self):
        _conf = self.conf.get("login_hint_lookup")
        if _conf:
            _userinfo = None
            _kwargs = _conf.get("kwargs")
            if _kwargs:
                _userinfo_conf = _kwargs.get("userinfo")
                if _userinfo_conf:
                    _userinfo = init_user_info(_userinfo_conf, self.cwd)

            if _userinfo is None:
                _userinfo = self.userinfo

            self.login_hint_lookup = init_service(_conf)
            self.login_hint_lookup.userinfo = _userinfo

    def do_userinfo(self):
        _conf = self.conf.get("userinfo")
        if _conf:
            if self.session_manager:
                self.userinfo = init_user_info(_conf, self.cwd)
                self.session_manager.userinfo = self.userinfo
            else:
                logger.warning("Cannot init_user_info if no session manager was provided.")

    def do_cookie_dealer(self):
        _conf = self.conf.get("cookie_dealer")
        if _conf:
            if not self.cookie_dealer:
                self.cookie_dealer = init_service(_conf)

    def do_sub_func(self) -> None:
        """
        Loads functions that creates subject "sub" values

        :return: string
        """
        _conf = self.conf.get("sub_func", {})
        for key, args in _conf.items():
            if "class" in args:
                self._sub_func[key] = init_service(args)
            elif "function" in args:
                if isinstance(args["function"], str):
                    self._sub_func[key] = importer(args["function"])
                else:
                    self._sub_func[key] = args["function"]

    def create_providerinfo(self, capabilities):
        """
        Dynamically create the provider info response

        :param capabilities:
        :return:
        """

        _provider_info = capabilities
        _provider_info["issuer"] = self.issuer
        _provider_info["version"] = "3.0"

        # acr_values
        if self.authn_broker:
            acr_values = self.authn_broker.get_acr_values()
            if acr_values is not None:
                _provider_info["acr_values_supported"] = acr_values

        if self.jwks_uri and self.keyjar:
            _provider_info["jwks_uri"] = self.jwks_uri

        _provider_info.update(self.idtoken.provider_info)
        if "scopes_supported" not in _provider_info:
            _provider_info["scopes_supported"] = [s for s in self.scope2claims.keys()]
        if "claims_supported" not in _provider_info:
            _provider_info["claims_supported"] = STANDARD_CLAIMS[:]

        return _provider_info