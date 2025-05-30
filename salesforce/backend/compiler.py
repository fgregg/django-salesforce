# django-salesforce
#
# by Hyneck Cernoch and Phil Christensen
# See LICENSE.md for details
#

"""
Generate queries using the SOQL dialect.  (like django.db.models.sql.compiler and  django.db.models.sql.where)
"""
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import re
import warnings
from django.core.exceptions import EmptyResultSet
from django.db import NotSupportedError
from django.db.models.sql import compiler as sql_compiler, where as sql_where, datastructures
from django.db.models.sql.constants import CURSOR, GET_ITERATOR_CHUNK_SIZE, MULTI, NO_RESULTS, SINGLE
from django.db.models.sql.where import AND
from django.db.transaction import TransactionManagementError

import salesforce.backend.models_lookups   # noqa pylint:disable=unused-import # required for activation of lookups
from salesforce.backend import DJANGO_30_PLUS, DJANGO_31_PLUS, DJANGO_40_PLUS, DJANGO_42_PLUS, DJANGO_52_PLUS
from salesforce.backend.utils import FullResultSet
from salesforce.dbapi import DatabaseError
from salesforce.dbapi.exceptions import SalesforceWarning
# pylint:disable=no-else-return,too-many-branches,too-many-locals

if DJANGO_52_PLUS:
    from django.db.models.sql.constants import ROW_COUNT  # type: ignore[attr-defined]
else:
    ROW_COUNT = "row count"

AliasMapItems = List[Tuple[
    Optional[str],
    str,
    Optional[Tuple[Tuple[str, str], ...]],
    str
]]

objects_needing_minimal_aliases = [
    'ContentDocumentLink', 'ContentFolderItem', 'ContentFolderMember', 'IdeaComment', 'Vote'
]


class SfParams:  # like an immutable DataClass: clone when updating
    def __init__(self):
        self.query_all = False
        self.all_or_none = None  # type: Optional[bool]
        self.edge_updates = False
        self.minimal_aliases = False


class SQLCompiler(sql_compiler.SQLCompiler):
    """
    A subclass of the default SQL compiler for the SOQL dialect.
    """
    soql_trans = None  # type: Optional[Dict[str, str]]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sf_params = SfParams()
        self.root_aliases = []  # type: List[str]

    def set_sf_params(self, sf_params: SfParams) -> 'SQLCompiler':
        self.sf_params = sf_params
        return self

    def get_from_clause(self) -> Tuple[List[str], List[Any]]:
        """
        Return the FROM clause, converted the SOQL dialect.

        It should be only the name of base object, even in parent-to-child and
        child-to-parent relationships queries.
        """
        self.query_topology()
        assert self.soql_trans
        if self.root_aliases and len(self.root_aliases) == 1:
            root_table = self.soql_trans[self.root_aliases[0]]
        else:
            sql_items, params = super().get_from_clause()
            assert not params
            root_table, alias = sql_items[0].rsplit(' ', 1)
            msg = "Only queries with one top child model are supported by Salesforce. Use a subquery."
            assert len(sql_items) == 1 and self.soql_trans.get(alias) == root_table, msg
        return [root_table], []

    def quote_name_unless_alias(self, name: str) -> str:
        """
        A wrapper around connection.ops.quote_name that doesn't quote aliases
        for table names. Mostly used during the ORDER BY clause.
        """
        r = self.connection.ops.quote_name(name)
        self.quote_cache[name] = r
        return r

    def sf_fix_field(self, sql_field: str, debug_: int = 0) -> str:
        """Translate the field name from sql join "alias.name" to SOQL tree "object_1.object_2...name"."""
        # debug_: 1 = print what is recompiled
        soql_trans = self.query_topology()
        if sql_field.startswith('('):
            return sql_field  # compiled and fixed yet
        if re.match(r'[\w.]+$', sql_field) and len(sql_field.split('.')) == 2:
            pre, field, post = '', sql_field, ''  # very easy fix for a simple 'select' field
        elif sql_field.startswith('COUNT(Id)') and re.match(r'^COUNT\(Id\)(?: \w+)?$', sql_field, re.ASCII):
            return sql_field  # not necessary to fix
        else:
            match = re.match(r'^((?:\w+\()*)'     # optional some nested functions with one parameter
                             r'((?:\w+\.)*\w+)'   # table.field
                             r'('
                             r'\)*'               # optional closing parentheses of functions
                             # alias or operator with a placeholder %s of a parameter or 'IN (...'
                             r'(?: (?:\w+|[!<>=]+ %s|LIKE %s|!?= null|(?:NOT )?IN \(.*))?)$',
                             sql_field, re.ASCII)
            if match:
                pre, field, post = match.groups()
                ok = len(field.rstrip('.Type').split('.')) == 2
            if not match or not ok:
                warnings.warn("sf_fix_field: Can not recompile unexpected sql: {!r}".format(sql_field),
                              SalesforceWarning)
                return sql_field
        # fix the field
        tab_name, field_name = field.split('.', 1)
        if self.sf_params.minimal_aliases or tab_name in objects_needing_minimal_aliases:
            assert len(self.root_aliases) == 1
            if tab_name == self.root_aliases[0]:
                trans_tab_name = ''
            else:
                trans_root = soql_trans[self.root_aliases[0]]
                assert soql_trans[tab_name].startswith(trans_root + '.')
                trans_tab_name = soql_trans[tab_name].replace(trans_root + '.', '', 1)
        else:
            trans_tab_name = soql_trans[tab_name]
        dot = '.' if trans_tab_name else ''
        ret = "%s%s%s%s%s" % (pre, trans_tab_name, dot, field_name, post)

        if debug_:
            print('** sf_fix_field: {!r} -> {!r}'.format(sql_field, ret))
        return ret

    # patched and simplified the parend method  # pylint:disable=no-else-return
    def execute_sql(self,
                    result_type: str = MULTI,
                    chunked_fetch: bool = False,
                    chunk_size: int = GET_ITERATOR_CHUNK_SIZE
                    ) -> Any:
        """
        Run the query against the database and returns the result(s). The
        return value is a single data item if result_type is SINGLE, or an
        iterator over the results if the result_type is MULTI.

        result_type is either MULTI (use fetchmany() to retrieve all rows),
        SINGLE (only retrieve a single row), or None. In this last case, the
        cursor is returned if any query is executed, since it's used by
        subclasses such as InsertQuery). It's possible, however, that no query
        is needed, as the filters describe an empty set. In that case, None is
        returned, to avoid any unnecessary database interaction.
        """
        result_type = result_type or NO_RESULTS
        try:
            sql, params = self.as_sql()
            if not sql:
                raise EmptyResultSet
        except EmptyResultSet:
            if result_type == MULTI:
                return iter([])
            else:
                return

        cursor = self.connection.cursor()
        cursor.prepare_query(self.query)
        cursor.execute(sql, params)

        if not result_type or result_type == 'cursor':
            return cursor

        if result_type == ROW_COUNT:
            try:
                return cursor.rowcount
            finally:
                cursor.close()
        if result_type == CURSOR:
            # Give the caller the cursor to process and close.
            return cursor
        if result_type == SINGLE:
            val = cursor.fetchone()
            cursor.close()
            return val
        if result_type == NO_RESULTS:
            cursor.close()
            return

        # The MULTI case.
        result: Iterable[Any] = iter(lambda: cursor.fetchmany(chunk_size),
                                     self.connection.features.empty_fetchmany_value)
        if not chunked_fetch and not self.connection.features.can_use_chunked_reads:
            # If we are using non-chunked reads, we return the same data
            # structure as normally, but ensure it is all read into memory
            # before going any further. Use chunked_fetch if requested.
            return list(result)
        return result
        # pylint:enable=no-else-return

    def as_sql(self, with_limits=True, with_col_aliases=False
               ) -> Tuple[str, Sequence[Any]]:  # pylint:disable=arguments-differ

        # pylint:disable=too-many-locals,too-many-branches,too-many-statements
        """
        Creates the SQL for this query. Returns the SQL string and list of
        parameters.

        If 'with_limits' is False, any limit/offset information is not included
        in the query.
        """
        assert isinstance(with_col_aliases, bool)
        # After executing the query, we must get rid of any joins the query
        # setup created. So, take note of alias counts before the query ran.
        # However we do not want to get rid of stuff done in pre_sql_setup(),
        # as the pre_sql_setup will modify query state in a way that forbids
        # another run of it.
        refcounts_before = self.query.alias_refcount.copy()
        try:
            extra_select, order_by, group_by = self.pre_sql_setup()
            if with_limits and self.query.low_mark == self.query.high_mark:
                return '', ()
            distinct_fields, distinct_params = self.get_distinct()

            # This must come after 'select', 'ordering', and 'distinct' -- see
            # docstring of get_from_clause() for details.
            from_, f_params = self.get_from_clause()

            try:
                where, w_params = self.compile(self.where) if self.where is not None else ("", [])
            except FullResultSet:
                where, w_params = "", []
            try:
                having, h_params = self.compile(self.having) if self.having is not None else ("", [])
            except FullResultSet:
                having, h_params = "", []
            params = []
            result = ['SELECT']

            if self.query.distinct:
                distinct_result, distinct_params = self.connection.ops.distinct_sql(
                    distinct_fields,
                    distinct_params,
                )
                result += distinct_result
                params += distinct_params

            out_cols = []
            col_idx = 1
            for _, (s_sql, s_params), alias in self.select + extra_select:
                s_sql = self.sf_fix_field(s_sql)
                if alias:
                    # fixed by removing 'AS' for aggregate
                    # alias is sometimes used in Django 5.2 also for normal fiels
                    # in a non trivial compiler, but such alias must be ignored
                    if s_sql.endswith(')'):
                        s_sql = '%s %s' % (s_sql, self.connection.ops.quote_name(alias))
                elif with_col_aliases:
                    s_sql = '%s AS %s' % (s_sql, 'Col%d' % col_idx)
                    col_idx += 1
                params.extend(s_params)
                out_cols.append(s_sql)

            result.append(', '.join(out_cols))

            result.append('FROM')
            result.extend(from_)
            params.extend(f_params)

            if where:
                result.append('WHERE %s' % where)
                params.extend(w_params)

            grouping = []
            for g_sql, g_params in group_by:
                grouping.append(g_sql)
                params.extend(g_params)
            if grouping:
                if distinct_fields:
                    raise NotSupportedError(
                        "annotate() + distinct(fields) is not implemented.")
                if not order_by:
                    order_by = self.connection.ops.force_no_ordering()
                grouping = [self.sf_fix_field(x) for x in grouping]
                result.append('GROUP BY %s' % ', '.join(grouping))

            if having:
                result.append('HAVING %s' % having)
                params.extend(h_params)

            if DJANGO_40_PLUS:
                if self.query.explain_info:                # type: ignore[attr-defined]
                    result.insert(0, self.connection.ops.explain_query_prefix(
                        self.query.explain_info.format,    # type: ignore[attr-defined]
                        **self.query.explain_info.options  # type: ignore[attr-defined]
                    ))
            else:
                if self.query.explain_query:
                    result.insert(0, self.connection.ops.explain_query_prefix(
                        self.query.explain_format,
                        **self.query.explain_options
                    ))

            if order_by:
                ordering = []
                for _, (o_sql, o_params, _) in order_by:
                    o_sql = self.sf_fix_field(o_sql)
                    ordering.append(o_sql)
                    params.extend(o_params)
                result.append('ORDER BY %s' % ', '.join(ordering))

            if with_limits:
                if self.query.high_mark is not None:
                    result.append('LIMIT %d' % (self.query.high_mark - self.query.low_mark))
                if self.query.low_mark:
                    if self.query.high_mark is None:
                        val = self.connection.ops.no_limit_value()
                        if val:
                            result.append('LIMIT %d' % val)
                    result.append('OFFSET %d' % self.query.low_mark)

            if self.query.select_for_update and self.connection.features.has_select_for_update:
                if self.connection.get_autocommit():
                    raise TransactionManagementError(
                        "select_for_update cannot be used outside of a transaction."
                    )

                # If we've been asked for a NOWAIT query but the backend does
                # not support it, raise a DatabaseError otherwise we could get
                # an unexpected deadlock.
                nowait = self.query.select_for_update_nowait
                if nowait and not self.connection.features.has_select_for_update_nowait:
                    raise DatabaseError('NOWAIT is not supported on this database backend.')
                result.append(self.connection.ops.for_update_sql(nowait=nowait))

            if self.query.model and getattr(self.query.model._meta, 'sf_tooling_api_model', False):
                assert self.query
                result = [x.replace(self.query.model._meta.db_table + '.', '') for x in result]
            return ' '.join(result), tuple(params)
        finally:
            # Finally do cleanup - get rid of the joins we created above.
            self.query.reset_refcounts(refcounts_before)

    def query_topology(self, _alias_map_items: Optional[AliasMapItems] = None) -> Dict[str, str]:
        # pylint:disable=too-many-locals,too-many-branches
        # SOQL for SFDC requires:
        # - multiple (N-1) relations between (N) tables are possible
        # - exactly one top controlling table
        # - every relation is a join from exactly one foreign key to
        #   one primary key named "Id".
        #
        # Reorder relations to be from the left to the right
        if self.soql_trans is not None:
            return self.soql_trans
        if not _alias_map_items and not self.query.alias_map:
            # empty alias_map is possible due to field expr
            return {}
        # Unified interface:
        #   alias_map_items = [(lhs, table, join_cols_, rhs),...]
        query = self.query
        if _alias_map_items:
            alias_map_items = _alias_map_items
        else:
            alias_map_items = []
            for v in query.alias_map.values():
                assert v.table_alias
                if isinstance(v, datastructures.Join):
                    alias_map_items.append((v.parent_alias, v.table_name, v.join_cols, v.table_alias))
                else:
                    alias_map_items.append((None, v.table_name, None, v.table_alias))
        # Analyze
        alias2table = {}  # Dict[str, str]
        side_l, side_r = set(), set()
        for (lhs, table, join_cols_, rhs) in alias_map_items:
            alias2table[rhs] = table
            if lhs is not None:
                assert join_cols_
                (join_cols,) = join_cols_  # length == 1 because primary key is one field
                assert len(join_cols) == 2
                # swap left-right if necessary. The left should be the top.
                if join_cols[0] == 'Id':
                    assert join_cols[1] != 'Id'
                    lhs, rhs = rhs, lhs
                    join_cols = join_cols[1], join_cols[0]
                assert join_cols[1] == 'Id'
                side_l.add(lhs)
                side_r.add(rhs)
            else:
                side_l.add(rhs)
        assert len(alias2table) == len(alias_map_items)
        # Recognize the top table
        assert len(side_l.union(side_r)) == len(alias_map_items)
        self.root_aliases = list(set(side_l).difference(side_r))
        # self.root_aliases = [x for x in top_lhs_set if alias2table[x] == query.model._meta.db_table]
        # translation rules into SOQL
        soql_trans = {top_lhs: alias2table[top_lhs] for top_lhs in self.root_aliases}
        work_lhses = set(self.root_aliases)
        while work_lhses:
            new_work = set()
            for (lhs, table, join_cols_, rhs) in alias_map_items:
                if lhs is not None:
                    assert join_cols_
                    (join_cols,) = join_cols_
                    if join_cols[0] == 'Id':
                        # swap lhs rhs
                        lhs, rhs = rhs, lhs
                        join_cols = join_cols[1], join_cols[0]
                    if lhs in work_lhses:
                        assert rhs not in soql_trans
                        if join_cols[0].endswith('__c'):
                            fkey = re.sub('__c$', '__r', join_cols[0])
                        else:
                            assert join_cols[0].endswith('Id')
                            fkey = re.sub('Id$', '', join_cols[0])
                        soql_trans[rhs] = '%s.%s' % (soql_trans[lhs], fkey)
                        new_work.add(rhs)
            work_lhses = new_work
        assert len(soql_trans) == len(alias_map_items)
        self.soql_trans = soql_trans
        return self.soql_trans


class SalesforceWhereNode(sql_where.WhereNode):

    # patched "django.db.models.sql.where.WhereNode.as_sql" from Django 2.1
    # pylint:disable=no-else-return,no-else-raise,too-many-branches,too-many-locals,unused-argument
    def as_salesforce(self, compiler: sql_compiler.SQLCompiler, connection) -> Tuple[str, List[Any]]:
        """
        Return the SQL version of the where clause and the value to be
        substituted in. Return '', [] if this node matches everything,
        None, [] if this node is empty, and raise EmptyResultSet if this
        node can't match anything.
        """

        # *** patch 1 (add) begin
        # # prepare SOQL translations
        if not isinstance(compiler, SQLCompiler):
            return super().as_sql(compiler, connection)
        # *** patch 1 end

        result = []
        result_params = []  # type: List[Any]
        if self.connector == AND:
            full_needed, empty_needed = len(self.children), 1
        else:
            full_needed, empty_needed = 1, len(self.children)

        for child in self.children:
            try:
                sql, params = compiler.compile(child)
            except EmptyResultSet:
                empty_needed -= 1
            except FullResultSet:
                full_needed -= 1
            else:
                if sql:

                    # *** patch 2 (add) begin
                    # # translate the alias of child to SOQL name
                    sql = compiler.sf_fix_field(sql)
                    # *** patch 2 end

                    result.append(sql)
                    result_params.extend(params)
                else:
                    full_needed -= 1
            # Check if this node matches nothing or everything.
            # First check the amount of full nodes and empty nodes
            # to make this node empty/full.
            # Now, check if this node is full/empty using the
            # counts.
            if empty_needed == 0:
                if self.negated:
                    if DJANGO_42_PLUS:
                        raise FullResultSet
                    return '', []
                else:
                    raise EmptyResultSet
            if full_needed == 0:
                if self.negated:
                    raise EmptyResultSet
                else:
                    if DJANGO_42_PLUS:
                        raise FullResultSet
                    return '', []
        conn = ' %s ' % self.connector
        sql_string = conn.join(result)
        if DJANGO_42_PLUS and not sql_string:
            raise FullResultSet
        if sql_string:
            if self.negated:
                # *** patch 3 (remove) begin
                # # Some backends (Oracle at least) need parentheses
                # # around the inner SQL in the negated case, even if the
                # # inner SQL contains just a single expression.
                # sql_string = 'NOT (%s)' % sql_string
                # *** patch 3 (add)
                # SOQL requires parentheses around "NOT" expression, if combined with AND/OR
                sql_string = '(NOT (%s))' % sql_string
                # *** patch 3 end
            elif len(result) > 1 or self.resolved:
                sql_string = '(%s)' % sql_string
        return sql_string, result_params
    # pylint:enable=no-else-return,too-many-branches,too-many-locals,unused-argument


class SQLInsertCompiler(sql_compiler.SQLInsertCompiler, SQLCompiler):  # type: ignore[misc] # noqa # as_sql

    if DJANGO_31_PLUS:

        def execute_sql(self, returning_fields=None):
            # copied from Django 3.1, with one line patch
            assert not (
                returning_fields and len(self.query.objs) != 1 and
                not self.connection.features.can_return_rows_from_bulk_insert
            )
            self.returning_fields = returning_fields
            with self.connection.cursor() as cursor:
                # this line is the added patch:
                cursor.prepare_query(self.query)
                for sql, params in self.as_sql():
                    cursor.execute(sql, params)
                if not self.returning_fields:
                    return []
                if self.connection.features.can_return_rows_from_bulk_insert and len(self.query.objs) > 1:
                    return self.connection.ops.fetch_returned_insert_rows(cursor)
                if self.connection.features.can_return_columns_from_insert:
                    assert len(self.query.objs) == 1
                    return [self.connection.ops.fetch_returned_insert_columns(cursor, self.returning_params)]
                return [(self.connection.ops.last_insert_id(
                    cursor, self.query.get_meta().db_table, self.query.get_meta().pk.column
                ),)]

    elif DJANGO_30_PLUS:

        def execute_sql(self, returning_fields=None):
            # copied from Django 3.0, with one line patch
            assert not (
                returning_fields and len(self.query.objs) != 1 and
                not self.connection.features.can_return_rows_from_bulk_insert
            )
            self.returning_fields = returning_fields
            with self.connection.cursor() as cursor:
                # this line is the added patch:
                cursor.prepare_query(self.query)
                for sql, params in self.as_sql():
                    cursor.execute(sql, params)
                if not self.returning_fields:
                    return []
                if self.connection.features.can_return_rows_from_bulk_insert and len(self.query.objs) > 1:
                    return self.connection.ops.fetch_returned_insert_rows(cursor)
                if self.connection.features.can_return_columns_from_insert:
                    if (
                            len(self.returning_fields) > 1 and
                            not self.connection.features.can_return_multiple_columns_from_insert
                    ):
                        raise NotSupportedError(
                            'Returning multiple columns from INSERT statements is '
                            'not supported on this database backend.'
                        )
                    assert len(self.query.objs) == 1
                    return self.connection.ops.fetch_returned_insert_columns(cursor)
                return [self.connection.ops.last_insert_id(
                    cursor, self.query.get_meta().db_table, self.query.get_meta().pk.column
                )]

    else:

        def execute_sql(self, return_id=False):  # type: ignore[misc] # noqa pylint:disable=arguments-differ
            # copied from Django 1.11, with one line patch
            assert not (
                return_id and len(self.query.objs) != 1 and
                not self.connection.features.can_return_ids_from_bulk_insert
            )
            self.return_id = return_id  # pylint:disable=attribute-defined-outside-init
            with self.connection.cursor() as cursor:
                # this line is the added patch:
                cursor.prepare_query(self.query)
                for sql, params in self.as_sql():
                    cursor.execute(sql, params)
                if not (return_id and cursor):
                    return
                if self.connection.features.can_return_ids_from_bulk_insert and len(self.query.objs) > 1:
                    return self.connection.ops.fetch_returned_insert_ids(cursor)
                if self.connection.features.can_return_id_from_insert:
                    assert len(self.query.objs) == 1
                    return self.connection.ops.fetch_returned_insert_id(cursor)
                return self.connection.ops.last_insert_id(
                    cursor, self.query.get_meta().db_table, self.query.get_meta().pk.column
                )


class SQLDeleteCompiler(sql_compiler.SQLDeleteCompiler, SQLCompiler):  # type: ignore[misc] # noqa # as_sql
    pass


class SQLUpdateCompiler(sql_compiler.SQLUpdateCompiler, SQLCompiler):  # type: ignore[misc] # noqa # as_sql,execute_sql
    pass


class SQLAggregateCompiler(sql_compiler.SQLAggregateCompiler, SQLCompiler):  # type: ignore[misc] # noqa # as_sql
    pass
