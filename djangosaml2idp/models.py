import json
import logging
from typing import Dict
import datetime
from django.conf import settings
from django.db import models
from django.utils.safestring import mark_safe
from saml2 import xmldsig
from django.utils.functional import cached_property
import os
from .idp import IDP

logger = logging.getLogger(__name__)

default_attribute_mapping = {
    # DJANGO: SAML
    'email': 'email',
    'first_name': 'first_name',
    'last_name': 'last_name',
    'is_staff': 'is_staff',
    'is_superuser':  'is_superuser',
}


class ServiceProvider(models.Model):
    # Bookkeeping
    dt_created = models.DateTimeField(verbose_name='Created at', auto_now_add=True)
    dt_updated = models.DateTimeField(verbose_name='Updated at', auto_now=True, null=True, blank=True)

    # Identification
    entity_id = models.CharField(verbose_name='Entity ID', max_length=256, unique=True)
    pretty_name = models.CharField(verbose_name='Pretty Name', blank=True, max_length=256, help_text='For display purposes, can be empty')
    description = models.TextField(verbose_name='Description', blank=True)
    metadata = models.TextField(verbose_name='Metadata XML', blank=True, help_text='XML containing the metadata')

    # Configuration
    active = models.BooleanField(verbose_name='Active', default=True)
    _processor = models.CharField(verbose_name='Processor', max_length=256, help_text='Import string for the (access) Processor to use.', default='djangosaml2idp.processors.BaseProcessor')
    _attribute_mapping = models.TextField(verbose_name='Attribute mapping', default=json.dumps(default_attribute_mapping), help_text='dict with the mapping from django attributes to saml attributes in the identity.')

    _nameid_field = models.CharField(verbose_name='NameID Field', blank=True, max_length=64, help_text='Attribute on the user to use as identifier during the NameID construction. Can be a callable. If not set, this will default to settings.SAML_IDP_DJANGO_USERNAME_FIELD; if that is not set, it will use the `USERNAME_FIELD` attribute on the active user model.')

    _sign_response = models.BooleanField(verbose_name='Sign response', blank=True, null=True, help_text='If not set, default to the "sign_response" setting of the IDP. If that one is not set, default to False.')
    _sign_assertion = models.BooleanField(verbose_name='Sign assertion', blank=True, null=True, help_text='If not set, default to the "sign_assertion" setting of the IDP. If that one is not set, default to False.')

    _signing_algorithm = models.CharField(verbose_name='Signing algorithm', blank=True, null=True, max_length=256, choices=[(constant, pretty) for (pretty, constant) in xmldsig.SIG_ALLOWED_ALG], help_text='If not set, use settings.SAML_AUTHN_SIGN_ALG.')
    _digest_algorithm = models.CharField(verbose_name='Digest algorithm', blank=True, null=True, max_length=256, choices=[(constant, pretty) for (pretty, constant) in xmldsig.DIGEST_ALLOWED_ALG], help_text='If not set, default to settings.SAML_AUTHN_DIGEST_ALG.')

    _encrypt_saml_responses = models.BooleanField(verbose_name='Encrypt SAML Response', null=True, help_text='If not set, default to settings.SAML_ENCRYPT_AUTHN_RESPONSE. If that one is not set, default to False.')

    class Meta:
        verbose_name = "Service Provider"
        verbose_name_plural = "Service Providers"
        indexes = [
            models.Index(fields=['entity_id', ]),
        ]

    def __str__(self):
        if self.pretty_name:
            return f'{self.pretty_name} ({self.entity_id})'
        return f'{self.entity_id}'

    @property
    def attribute_mapping(self) -> Dict[str, str]:
        if not self._attribute_mapping:
            return default_attribute_mapping
        return json.loads(self._attribute_mapping)

    @property
    def nameid_field(self) -> str:
        if self._nameid_field:
            return self._nameid_field
        if hasattr(settings, 'SAML_IDP_DJANGO_USERNAME_FIELD'):
            return settings.SAML_IDP_DJANGO_USERNAME_FIELD
        return getattr(settings.AUTH_USER_MODEL, 'USERNAME_FIELD', 'username')

    # Do checks on validity of processor string both on setting and getting, as the
    # codebase can change regardless of the objects persisted in the database.

    @cached_property
    def processor(self) -> 'BaseProcessor':
        from .processors import validate_processor_path, instantiate_processor
        processor_cls = validate_processor_path(self._processor)
        return instantiate_processor(processor_cls, self.entity_id)

    def metadata_path(self) -> str:
        """ Write the metadata content to a local file, so it can be used as 'local'-type metadata for pysaml2. """
        path = '/tmp/djangosaml2idp'
        if not os.path.exists(path):
            try:
                os.mkdir(path)
            except Exception as e:
                logger.error(f'Could not create temporary folder to store metadata at {path}: {e}')
                raise
        filename = f'{path}/{self.id}.xml'
        if not os.path.exists(filename) or self.dt_updated > datetime.datetime.fromtimestamp(os.path.getmtime(filename)):
            try:
                with open(filename, 'w') as f:
                    f.write(self.metadata)
            except Exception as e:
                logger.error(f'Could not write metadata to file {filename}: {e}')
                raise
        return filename

    @property
    def sign_response(self) -> bool:
        if self._sign_response is None:
            return getattr(IDP.load().config, "sign_response", False)
        return self._sign_response

    @property
    def sign_assertion(self) -> bool:
        if self._sign_assertion is None:
            return getattr(IDP.load().config, "sign_assertion", False)
        return self._sign_assertion

    @property
    def encrypt_saml_responses(self) -> bool:
        if self._encrypt_saml_responses is None:
            return getattr(settings, 'SAML_ENCRYPT_AUTHN_RESPONSE', False)
        return self._encrypt_saml_responses

    @property
    def signing_algorithm(self) -> str:
        if self._signing_algorithm is None:
            return settings.SAML_AUTHN_SIGN_ALG
        return self._signing_algorithm

    @property
    def digest_algorithm(self) -> str:
        if self._digest_algorithm is None:
            return settings.SAML_AUTHN_DIGEST_ALG
        return self._digest_algorithm

    @property
    def resulting_config(self) -> str:
        """ Actual values of the config / properties with the settings and defaults taken into account.
        """
        d = {
            'entity_id': self.entity_id,
            'attribute_mapping': self.attribute_mapping,
            'nameid_field': self.nameid_field,
            'sign_response': self.sign_response,
            'sign_assertion': self.sign_assertion,
            'encrypt_saml_responses': self.encrypt_saml_responses,
            'signing_algorithm': self.signing_algorithm,
            'digest_algorithm': self.digest_algorithm,
        }
        config_as_str = json.dumps(d, indent=4)
        # Some ugly replacements to have the json decently printed in the admin
        return mark_safe(config_as_str.replace("\n", "<br>").replace("    ", "&nbsp;&nbsp;&nbsp;&nbsp;"))
