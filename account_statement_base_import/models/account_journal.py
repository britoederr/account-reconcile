# -*- coding: utf-8 -*-
# © 2011-2016 Camptocamp SA
#             Joel Grand-Guillaume, Nicolas Bessi, Matthieu Dietrich
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import sys
import traceback
import os
from openerp import _, api, fields, models
from ..parser.parser import new_move_parser
from openerp.exceptions import UserError, ValidationError
from operator import attrgetter


class AccountJournal(models.Model):
    _name = 'account.journal'
    _inherit = ['account.journal', 'mail.thread']

    def _get_import_type_selection(self):
        """This is the method to be inherited for adding the parser"""
        return [('generic_csvxls_so', 'Generic .csv/.xls based on SO Name')]

    def __get_import_type_selection(self):
        """ Call method which can be inherited """
        return self._get_import_type_selection()

    used_for_import = fields.Boolean(
        string="Journal used for import")

    commission_account_id = fields.Many2one(
        comodel_name='account.account',
        string='Commission account')

    import_type = fields.Selection(
        __get_import_type_selection,
        string='Type of import',
        default='generic_csvxls_so',
        required=True,
        help="Choose here the method by which you want to import bank"
        "statement for this profile.")

    last_import_date = fields.Datetime(
        string="Last Import Date")

    partner_id = fields.Many2one(
        comodel_name='res.partner',
        string='Bank/Payment Office partner',
        help="Put a partner if you want to have it on the commission move "
        "(and optionaly on the counterpart of the intermediate/"
        "banking move if you tick the corresponding checkbox).")

    receivable_account_id = fields.Many2one(
        comodel_name='account.account',
        string='Force Receivable/Payable Account',
        help="Choose a receivable account to force the default "
        "debit/credit account (eg. an intermediat bank account "
        "instead of default debitors).")

    used_for_completion = fields.Boolean(
        string="Journal used for completion")

    rule_ids = fields.Many2many(
        comodel_name='account.move.completion.rule',
        string='Auto-completion rules',
        rel='as_rul_st_prof_rel')

    launch_import_completion = fields.Boolean(
        string="Launch completion after import",
        help="Tic that box to automatically launch the completion "
        "on each imported file using this profile.")

    def _get_rules(self):
        # We need to respect the sequence order
        return sorted(self.rule_ids, key=attrgetter('sequence'))

    def _find_values_from_rules(self, calls, line):
        """This method will execute all related rules, in their sequence order,
        to retrieve all the values returned by the first rules that will match.
        :param calls: list of lookup function name available in rules
        :param dict line: read of the concerned account.bank.statement.line
        :return:
            A dict of value that can be passed directly to the write method of
            the statement line or {}
           {'partner_id': value,
            'account_id: value,
            ...}
        """
        if not calls:
            calls = self._get_rules()
        rule_obj = self.env['account.move.completion.rule']
        for call in calls:
            method_to_call = getattr(rule_obj, call.function_to_call)
            result = method_to_call(line)
            if result:
                result['already_completed'] = True
                return result
        return None

    @api.multi
    def _write_extra_move_lines(self, parser, move):
        """Insert extra lines after the main statement lines.

        After the main statement lines have been created, you can override this
        method to create extra statement lines.

            :param:    browse_record of the current parser
            :param:    result_row_list: [{'key':value}]
            :param:    profile: browserecord of account.statement.profile
            :param:    statement_id: int/long of the current importing
              statement ID
            :param:    context: global context
        """
        move_line_obj = self.env['account.move.line']
        global_commission_amount = 0
        total_amount = 0
        for row in parser.result_row_list:
            global_commission_amount += float(
                row.get('commission_amount', '0.0'))
            total_amount += float(
                row.get('amount', '0.0'))
        total_amount += global_commission_amount
        partner_id = self.partner_id.id
        # Commission line
        if global_commission_amount < 0.0:
            commission_account_id = self.commission_account_id.id
            comm_values = {
                'name': _('Commission line'),
                'date_maturity': parser.get_move_vals().get('date') or
                fields.Date.today(),
                'debit': -global_commission_amount,
                'partner_id': partner_id,
                'move_id': move.id,
                'account_id': commission_account_id,
                'already_completed': True,
            }
            move_line_obj.with_context(check_move_validity=False).create(comm_values)
        # Counterpart line
        if total_amount > 0.0:
            receivable_account_id = self.receivable_account_id.id or False
            counterpart_values = {
                'name': _('Counterpart line'),
                'date_maturity': parser.get_move_vals().get('date') or
                fields.Date.today(),
                'debit': total_amount,
                'partner_id': partner_id,
                'move_id': move.id,
                'account_id': receivable_account_id,
                'already_completed': True,
            }
            move_line_obj.create(counterpart_values)

    @api.multi
    def write_logs_after_import(self, move, num_lines):
        """Write the log in the logger

        :param int/long statement_id: ID of the concerned
          account.bank.statement
        :param int/long num_lines: Number of line that have been parsed
        :return: True
        """
        self.message_post(
            body=_('Move %s have been imported with %s '
                   'lines.') % (move.name, num_lines))
        return True

    def prepare_move_line_vals(self, parser_vals, move):
        """Hook to build the values of a line from the parser returned values.
        At least it fullfill the basic values. Overide it to add your own
        completion if needed.

        :param dict of vals from parser for account.bank.statement.line
          (called by parser.get_st_line_vals)
        :param int/long statement_id: ID of the concerned
          account.bank.statement
        :return: dict of vals that will be passed to create method of
          statement line.
        """
        move_line_obj = self.env['account.move.line']
        values = parser_vals
        values['company_id'] = self.company_id.id
        values['currency_id'] = self.currency_id.id
        values['company_currency_id'] = self.company_id.currency_id.id
        values['journal_id'] = self.id
        values['move_id'] = move.id
        if values['credit'] > 0.0:
            values['account_id'] = self.default_credit_account_id.id
        else:
            values['account_id'] = self.default_debit_account_id.id
        values = move_line_obj._add_missing_default_values(values)
        return values

    def prepare_move_vals(self, result_row_list, parser):
        """Hook to build the values of the statement from the parser and
        the profile.
        """
        vals = {'journal_id': self.id,
                'currency_id': self.currency_id.id}
        vals.update(parser.get_move_vals())
        return vals

    def multi_move_import(self, file_stream, ftype="csv"):
        """Create multiple bank statements from values given by the parser for
        the given profile.

        :param int/long profile_id: ID of the profile used to import the file
        :param filebuffer file_stream: binary of the providen file
        :param char: ftype represent the file exstension (csv by default)
        :return: list: list of ids of the created account.bank.statemênt
        """
        filename = self._context.get('file_name', None)
        if filename:
            (filename, __) = os.path.splitext(filename)
        parser = new_move_parser(self, ftype=ftype, move_ref=filename)
        res = self.env['account.move']
        for result_row_list in parser.parse(file_stream):
            move = self._move_import(parser, file_stream, ftype=ftype)
            res |= move
        return res

    def _move_import(self, parser, file_stream, ftype="csv"):
        """Create a bank statement with the given profile and parser. It will
        fullfill the bank statement with the values of the file providen, but
        will not complete data (like finding the partner, or the right
        account). This will be done in a second step with the completion rules.

        :param prof : The profile used to import the file
        :param parser: the parser
        :param filebuffer file_stream: binary of the providen file
        :param char: ftype represent the file exstension (csv by default)
        :return: ID of the created account.bank.statemênt
        """
        move_obj = self.env['account.move']
        move_line_obj = self.env['account.move.line']
        attachment_obj = self.env['ir.attachment']
        result_row_list = parser.result_row_list
        # Check all key are present in account.bank.statement.line!!
        if not result_row_list:
            raise UserError(_("Nothing to import: "
                              "The file is empty"))
        parsed_cols = parser.get_move_line_vals(result_row_list[0]).keys()
        for col in parsed_cols:
            if col not in move_line_obj._columns:
                raise UserError(
                    _("Missing column! Column %s you try to import is not "
                      "present in the bank statement line!") % col)
        move_vals = self.prepare_move_vals(result_row_list, parser)
        move = move_obj.create(move_vals)
        try:
            # Record every line in the bank statement
            move_store = []
            for line in result_row_list:
                parser_vals = parser.get_move_line_vals(line)
                values = self.prepare_move_line_vals(parser_vals, move)
                move_store.append(values)
            # Hack to bypass ORM poor perfomance. Sob...
            move_line_obj._insert_lines(move_store)
            self._write_extra_move_lines(parser, move)
            attachment_data = {
                'name': 'statement file',
                'datas': file_stream,
                'datas_fname': "%s.%s" % (fields.Date.today(), ftype),
                'res_model': 'account.move',
                'res_id': move.id,
            }
            attachment_obj.create(attachment_data)
            # If user ask to launch completion at end of import, do it!
            if self.launch_import_completion:
                move.button_auto_completion()
            # Write the needed log infos on profile
            self.write_logs_after_import(move, len(result_row_list))
        except Exception:
            error_type, error_value, trbk = sys.exc_info()
            st = "Error: %s\nDescription: %s\nTraceback:" % (
                error_type.__name__, error_value)
            st += ''.join(traceback.format_tb(trbk, 30))
            raise ValidationError(
                _("Statement import error"
                  "The statement cannot be created: %s") % st)
        return move
