from decimal import Decimal
import logging
import stripe
from django.conf import settings
from django.utils.translation import ugettext as _
from corehq import Domain
from corehq.apps.accounting.models import (
    BillingAccount,
    CreditLine,
    Invoice,
    PaymentRecord,
    SoftwareProductType,
    FeatureType,
)
from corehq.apps.accounting.user_text import get_feature_name
from corehq.apps.accounting.utils import fmt_dollar_amount
from corehq.const import USER_DATE_FORMAT
from dimagi.utils.decorators.memoized import memoized

stripe.api_key = settings.STRIPE_PRIVATE_KEY
logger = logging.getLogger('accounting')

def get_or_create_stripe_customer(payment_method):
    customer = None
    if payment_method.customer_id is not None:
        try:
            customer = stripe.Customer.retrieve(payment_method.customer_id)
        except stripe.InvalidRequestError:
            pass
    if customer is None:
        customer = stripe.Customer.create(
            description = "{}'s cards".format(payment_method.web_user),
            email=payment_method.web_user,
        )
    payment_method.customer_id = customer.id
    payment_method.save()
    return customer


class BaseStripePaymentHandler(object):
    """Handler for paying via Stripe's API
    """
    receipt_email_template = None
    receipt_email_template_plaintext = None

    def __init__(self, payment_method, domain):
        self.payment_method = payment_method
        self.domain = domain

    @property
    def cost_item_name(self):
        """Returns a name for the cost item that's used in the logging messages.
        """
        raise NotImplementedError("you must implement cost_item_name")

    @property
    @memoized
    def core_product(self):
        domain = Domain.get_by_name(self.domain)
        return SoftwareProductType.get_type_by_domain(domain)

    def create_charge(self, amount, card=None, customer=None):
        """Process the HTTPRequest used to make this payment

        returns a dict to be used as the json response for the request.
        """
        raise NotImplementedError("you must implement process_request")

    def get_charge_amount(self, request):
        """Returns a Decimal of the amount to be charged.
        """
        raise NotImplementedError("you must implement get_charge_amount")

    def update_credits(self, payment_record):
        """Updates any relevant Credit lines
        """
        raise NotImplementedError("you must implement update_credits")

    def get_amount_in_cents(self, amount):
        amt_cents = amount * Decimal('100')
        return int(amt_cents.quantize(Decimal(10)))

    def process_request(self, request):
        customer = None
        amount = self.get_charge_amount(request)
        card = request.POST.get('stripeToken')
        remove_card = request.POST.get('removeCard')
        is_saved_card = request.POST.get('selectedCardType') == 'saved'
        save_card = request.POST.get('saveCard') and not is_saved_card
        generic_error = {
            'error': {
                'message': _(
                    "Something went wrong while processing your payment. "
                    "We're working quickly to resolve the issue. No charges "
                    "were issued. Please try again in a few hours."
                ),
            },
        }
        try:
            if remove_card:
                customer = get_or_create_stripe_customer(self.payment_method)
                customer.cards.retrieve(card).delete()
                return {
                    'success': True,
                    'removedCard': card,
                }
            if save_card:
                customer = get_or_create_stripe_customer(self.payment_method)
                card = customer.cards.create(card=card)
                customer.default_card = card
                customer.save()
                card = card
            if is_saved_card:
                customer = get_or_create_stripe_customer(self.payment_method)
            charge = self.create_charge(amount, card=card, customer=customer)
            payment_record = PaymentRecord.create_record(
                self.payment_method, charge.id, amount
            )
            self.update_credits(payment_record)
            try:
                self.send_email(payment_record)
            except Exception:
                logger.error(
                    "[BILLING] Failed to send out an email receipt for "
                    "payment related to PaymentRecord No. %s. "
                    "Everything else succeeded."
                    % payment_record.id, exc_info=True
                )
        except stripe.error.CardError as e:
            # card was declined
            return e.json_body
        except (
            stripe.error.AuthenticationError,
            stripe.error.InvalidRequestError,
            stripe.error.APIConnectionError,
            stripe.error.StripeError,
        ) as e:
            logger.error(
                "[BILLING] A payment for %(cost_item)s failed due "
                "to a Stripe %(error_class)s: %(error_msg)s" % {
                    'error_class': e.__class__.__name__,
                    'cost_item': self.cost_item_name,
                    'error_msg': e.json_body['error']
                }, exc_info=True)
            return generic_error
        except Exception as e:
            logger.error(
                "[BILLING] A payment for %(cost_item)s failed due "
                "to: %(error_msg)s" % {
                    'cost_item': self.cost_item_name,
                    'error_msg': e,
                }, exc_info=True)
            return generic_error
        return {
            'success': True,
            'card': card,
            'wasSaved': save_card,
        }

    def get_email_context(self):
        return {
            'invoicing_contact_email': settings.INVOICING_CONTACT_EMAIL,
        }

    def send_email(self, payment_record):
        additional_context = self.get_email_context()
        from corehq.apps.accounting.tasks import send_purchase_receipt
        send_purchase_receipt.delay(
            payment_record, self.core_product, self.receipt_email_template,
            self.receipt_email_template_plaintext, additional_context, self.domain
        )


class InvoiceStripePaymentHandler(BaseStripePaymentHandler):
    receipt_email_template = 'accounting/invoice_receipt_email.html'
    receipt_email_template_plaintext = 'accounting/invoice_receipt_email_plaintext.txt'

    def __init__(self, payment_method, domain, invoice):
        super(InvoiceStripePaymentHandler, self).__init__(payment_method, domain)
        self.invoice = invoice

    @property
    def cost_item_name(self):
        return _("Invoice #%s") % self.invoice.id

    def create_charge(self, amount, card=None, customer=None):
        return stripe.Charge.create(
            card=card,
            customer=customer,
            amount=self.get_amount_in_cents(amount),
            currency=settings.DEFAULT_CURRENCY,
            description="Payment for Invoice %s" % self.invoice.invoice_number,
        )

    def get_charge_amount(self, request):
        """Returns a Decimal of the amount to be charged.
        """
        if request.POST['paymentAmount'] == 'full':
            return self.invoice.balance.quantize(Decimal(10) ** -2)
        return Decimal(request.POST['customPaymentAmount'])

    def update_credits(self, payment_record):
        # record the credit to the account
        CreditLine.add_credit(
            payment_record.amount, account=self.invoice.subscription.account,
            payment_record=payment_record,
        )
        CreditLine.add_credit(
            -payment_record.amount,
            account=self.invoice.subscription.account,
            invoice=self.invoice,
        )
        self.invoice.update_balance()
        self.invoice.save()

    def get_email_context(self):
        context = super(InvoiceStripePaymentHandler, self).get_email_context()
        context.update({
            'balance': fmt_dollar_amount(self.invoice.balance),
            'is_paid': self.invoice.is_paid,
            'date_due': self.invoice.date_due.strftime(USER_DATE_FORMAT) if self.invoice.date_due else 'None',
            'invoice_num': self.invoice.invoice_number,
        })
        return context


class BulkStripePaymentHandler(BaseStripePaymentHandler):
    receipt_email_template = 'accounting/bulk_payment_receipt_email.html'
    receipt_email_template_plaintext = 'accounting/bulk_payment_receipt_email_plaintext.txt'

    def __init__(self, payment_method, domain):
        super(BulkStripePaymentHandler, self).__init__(payment_method, domain)

    @property
    def cost_item_name(self):
        return _('Bulk Payment for project space %s' % self.domain)

    def create_charge(self, amount, card=None, customer=None):
        return stripe.Charge.create(
            card=card,
            customer=customer,
            amount=self.get_amount_in_cents(amount),
            currency=settings.DEFAULT_CURRENCY,
            description=self.cost_item_name,
        )

    @property
    def invoices(self):
        return Invoice.objects.filter(
            subscription__subscriber__domain=self.domain,
            is_hidden=False,
        )

    @property
    def balance(self):
        return sum(invoice.balance for invoice in self.invoices)

    def get_charge_amount(self, request):
        if request.POST['paymentAmount'] == 'full':
            return self.balance
        return Decimal(request.POST['customPaymentAmount'])

    def update_credits(self, payment_record):
        amount = payment_record.amount
        for invoice in self.invoices:
            deduct_amount = min(amount, invoice.balance)
            amount -= deduct_amount
            if deduct_amount > 0:
                # TODO - refactor duplicated functionality
                CreditLine.add_credit(
                    deduct_amount, account=invoice.subscription.account,
                    payment_record=payment_record,
                )
                CreditLine.add_credit(
                    -deduct_amount,
                    account=invoice.subscription.account,
                    invoice=invoice,
                )
                invoice.update_balance()
                invoice.save()
        if amount:
            account = BillingAccount.get_or_create_account_by_domain(self.domain)
            CreditLine.add_credit(
                amount, account=account,
                payment_record=payment_record,
            )

    def get_email_context(self):
        context = super(BulkStripePaymentHandler, self).get_email_context()
        context.update({
            'is_paid': all(invoice.is_paid for invoice in self.invoices),
            'domain': self.domain,
            'balance': self.balance,
        })
        return context


class CreditStripePaymentHandler(BaseStripePaymentHandler):
    receipt_email_template = 'accounting/credit_receipt_email.html'
    receipt_email_template_plaintext = 'accounting/credit_receipt_email_plaintext.txt'

    def __init__(self, payment_method, domain, account, subscription=None, post_data=None):
        super(CreditStripePaymentHandler, self).__init__(payment_method, domain)
        self.features = [{'type': feature_type[0],
                          'amount': Decimal(post_data.get(feature_type[0], 0))}
                         for feature_type in FeatureType.CHOICES
                         if Decimal(post_data.get(feature_type[0], 0)) > 0]
        self.products = [{'type': product_type[0],
                          'amount': Decimal(post_data.get(product_type[0], 0))}
                         for product_type in SoftwareProductType.CHOICES
                         if Decimal(post_data.get(product_type[0], 0)) > 0]
        self.post_data = post_data
        self.account = account
        self.subscription = subscription
        self.credit_lines = []

    @property
    def cost_item_name(self):
        credit_types = [unicode(product['type']) for product in self._humanized_products()]
        credit_types += [unicode(feature['type']) for feature in self._humanized_features()]
        return _("Credits: {credit_types} for {sub_or_account}").format(
            credit_types=", ".join(credit_types),
            sub_or_account=("Subscription %s" % self.subscription
                            if self.subscription is None
                            else "Account %s" % self.account.id)
        )

    def _humanized_features(self):
        return [{'type': get_feature_name(feature['type'], self.core_product),
                 'amount': fmt_dollar_amount(feature['amount'])}
                for feature in self.features]

    def _humanized_products(self):
        return [{'type': product['type'],
                 'amount': fmt_dollar_amount(product['amount'])}
                for product in self.products]

    def get_charge_amount(self, request):
        return Decimal(request.POST['amount'])

    def create_charge(self, amount, card=None, customer=None):
        return stripe.Charge.create(
            card=card,
            customer=customer,
            amount=self.get_amount_in_cents(amount),
            currency=settings.DEFAULT_CURRENCY,
            description="Payment for %s" % self.cost_item_name,
        )

    def update_credits(self, payment_record):
        for feature in self.features:
            feature_amount = feature['amount']
            if feature_amount >= 0.5:
                self.credit_lines.append(CreditLine.add_credit(
                    feature_amount,
                    account=self.account,
                    subscription=self.subscription,
                    feature_type=feature['type'],
                    payment_record=payment_record,
                ))
            else:
                logger.error("[BILLING] {account} tried to make a payment for {feature} for less than $0.5."
                             "You should follow up with them.".format(account=self.account,
                                                                      feature=feature['type']))
        for product in self.products:
            plan_amount = product['amount']
            if plan_amount >= 0.5:
                self.credit_lines.append(CreditLine.add_credit(
                    plan_amount,
                    account=self.account,
                    subscription=self.subscription,
                    product_type=product['type'],
                    payment_record=payment_record,
                ))
            else:
                logger.error("[BILLING] {account} tried to make a payment for {product} for less than $0.5."
                             "You should follow up with them.".format(account=self.account,
                                                                      product=product['type']))

    def process_request(self, request):
        response = super(CreditStripePaymentHandler, self).process_request(request)
        if self.credit_lines:
            response.update({
                'balances': [{'type': cline.product_type if cline.product_type else cline.feature_type,
                              'balance': fmt_dollar_amount(cline.balance)}
                             for cline in self.credit_lines]
            })
        return response

    def get_email_context(self):
        context = super(CreditStripePaymentHandler, self).get_email_context()
        context.update({
            'items': self._humanized_products() + self._humanized_features()
        })
        return context
