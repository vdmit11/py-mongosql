from __future__ import absolute_import

from sqlalchemy.sql.expression import and_, or_, not_, cast
from sqlalchemy.sql import operators
from sqlalchemy.sql.functions import func

from sqlalchemy.dialects import postgresql as pg
from .base import _MongoQueryStatementBase
from ..bag import CombinedBag
from ..exc import InvalidQueryError, InvalidColumnError, InvalidRelationError


def _is_array(value):
    return isinstance(value, (list, tuple, set, frozenset))


class FilterExpressionBase(object):
    """ An expression from the MongoFilter object """

    def __init__(self, operator_str, value):
        self.operator_str = operator_str
        self.value = value

    def compile_expression(self):
        """ Compiles the expression into an SQL expression """
        raise NotImplementedError()

    @staticmethod
    def sql_anded_together(conditions):
        """ Take a list of conditions and AND then together into an SQL expression

            In a few places in the code, we keep conditions in a list without wrapping them
            explicitly into a Boolean expression: just to keep it simple, easy to go through.

            This method will put them together, as required.
        """
        # No conditions: just return True, which is a valid sqlalchemy expression for filtering
        if not conditions:
            return True

        # AND them together
        cc = and_(*conditions)
        # Put parentheses around it, if necessary
        return cc.self_group() if len(conditions) > 1 else cc


class FilterBooleanExpression(FilterExpressionBase):
    """ A boolean expression.

        Consists of: an operator ($and, etc), and a value (list of FilterExpressionBase)
    """

    def __init__(self, operator_str, value):
        """ Init a boolean expression

        :type operator_str: str
        :type value: FilterExpressionBase | list[FilterExpressionBase]
        """
        super(FilterBooleanExpression, self).__init__(operator_str, value)

    def __repr__(self):
        return '({}: {})'.format(self.operator_str, self.value)

    def compile_expression(self):
        # So, this is what we expect here
        # self.operator_str: $and, $or, $nor, $not
        # self.value: list[FilterExpressionBase], or just FilterExpressionBase for $not
        #   This means `value` is a list of (column, operator, value), wrapped into an object.
        #   For example: (`age`, ">=", 18)
        #   And the current boolean clause puts it together.

        if self.operator_str == '$not':
            # This operator accepts a FilterExpressionBase, not a list.
            criterion = self.sql_anded_together([
                c.compile_expression()
                for c in self.value
            ])
            return not_(criterion)
        else:
            # Those operators share some steps, so they've been put into one section

            # Their argument (self.value) is a list[FilterExpressionBase].
            # Compile it
            criteria = [self.sql_anded_together([c.compile_expression() for c in cs])
                        for cs in self.value]

            # Build an expression for the boolean operator
            if self.operator_str in ('$or', '$nor'):
                cc = or_(*criteria)
                # for $nor, it will be negated later
            elif self.operator_str == '$and':
                cc = and_(*criteria)
            else:
                raise NotImplementedError('Unknown operator: {}'.format(self.operator_str))

            # Put parentheses around it when there are multiple clauses
            cc = cc.self_group() if len(criteria) > 1 else cc

            # for $nor, we promised to negate the result
            if self.operator_str == '$nor':
                return ~cc
            # Done
            return cc


class FilterColumnExpression(FilterExpressionBase):
    """ An expression involving a column

        Consists of: an operator ($eq, etc), a column, and a value to compare the column to
    """
    def __init__(self,
                 bag, column_name, column,
                 operator_str, operator_lambda,
                 value):
        """ Init a column expression

        :param bag: the bag that contains information about the column
        :type bag: mongosql.bags.ColumnsBag
        :param column_name: Name of the column referenced (possibly, with a dot!)
        :param column: The actual column (reference)
        :param operator_str: The operator to use, e.g. $eq
        :param operator_lambda: A callable that implements an SQL expression handling the operator
        :param value: The value the operator is applied to
        """
        super(FilterColumnExpression, self).__init__(operator_str, value)
        self.bag = bag
        self.column_name = column_name
        self.column = column
        self.operator_lambda = operator_lambda

        # Those can be changed by preprocess_column_and_value() to do proper type casting
        self.column_expression = self.column
        self.value_expression = self.value

    def __repr__(self):
        return '{} {} {!r}'.format(self.column_name, self.operator_str, self.value)

    def is_column_array(self):
        return self.bag.is_column_array(self.column_name)

    def is_column_json(self):
        return self.bag.is_column_json(self.column_name)

    def is_value_array(self):
        return _is_array(self.value)

    def preprocess_column_and_value(self):
        """ Preprocess the column and the value

            Certain operations will only work if the types are cast correctly.
            This is where it happens.
        """
        col, val = self.column, self.value

        # Case 1. Both column and value are arrays
        if self.is_column_array() and self.is_value_array():
            # Cast the value to ARRAY[] with the same type that the column has
            # Only in this case Postgres will be able to handles them both
            val = cast(pg.array(val), pg.ARRAY(col.type.item_type))

        # Case 2. JSON column
        if self.is_column_json():
            # This is the type to which JSON column is coerced: same as `value`
            # Doc: "Suggest a type for a `coerced` Python value in an expression."
            coerce_type = col.type.coerce_compared_value('=', val)  # HACKY: use sqlalchemy type coercion
            # Now, replace the `col` used in operations with this new coerced expression
            col = cast(col, coerce_type)

        # Done
        self.column_expression = col
        self.value_expression = val

    def compile_expression(self):
        # Prepare
        self.preprocess_column_and_value()

        # Apply this operator to the column and value expressions, return the compiled statement
        return self.operator_lambda(
            self.column_expression,
            self.value_expression,
            self.value  # original value
        )


class FilterRelatedColumnExpression(FilterColumnExpression):
    """ An expression involving a related column (dot-notation: 'users.age') """

    def __init__(self,
                 bag, relation_name, relation,
                 column_name, column,
                 operator_str, operator_lambda,
                 value):
        """ Init a column expression involving a related column

        :type bag: mongosql.bags.DotRelatedColumnsBag
        :param relation_name: Name of the relationship the column is referenced through
        :param relation: The relationship
        """
        super(FilterRelatedColumnExpression, self).__init__(bag, column_name, column, operator_str, operator_lambda, value)
        self.relation_name = relation_name
        self.relation = relation


class MongoFilter(_MongoQueryStatementBase):
    """ MongoDB filter.

        This is essentially used for filtering, but it is also used in aggregation logic.
        For instance, if you want to count all people older than 18 years old,
        you would aggregate them using a criteria:
            {aggregate: {
                old_enough_count: { $sum: { age: { $gt: 18 } } }
            }}

        Supports the following MongoDB operators:

        * None: no op
        * { a: 1 }  - equality. For arrays: contains value.
        * { a: { $lt: 1 } }  - <
        * { a: { $lte: 1 } } - <=
        * { a: { $ne: 1 } } - <>. For arrays: does not contain value
        * { a: { $gte: 1 } } - >=
        * { a: { $gt: 1 } } - >
        * { a: { $in: [...] } } - any of. For arrays: has any from
        * { a: { $nin: [...] } } - none of. For arrays: has none from

        * { a: { $exists: true } } - is [not] NULL

        * { arr: { $all: [...] } } For arrays: contains all values
        * { arr: { $size: 0 } } For arrays: has a length of 0

        Supports the following boolean operators:

        * { $or: [ {..criteria..}, .. ] }  - any is true
        * { $and: [ {..criteria..}, .. ] } - all are true
        * { $nor: [ {..criteria..}, .. ] } - none is true
        * { $not: { ..criteria.. } } - negation

        Supported: Columns, Related Columns, Hybrid Properties
    """

    query_object_section_name = 'filter'

    def __init__(self, model, scalar_operators=None, array_operators=None):
        super(MongoFilter, self).__init__(model)

        # On input
        self.expressions = None

        # Extra configuration
        self._extra_scalar_ops = scalar_operators or {}
        self._extra_array_ops = array_operators or {}

    def _get_supported_bags(self):
        return CombinedBag(
            col=self.bags.columns,
            rcol=self.bags.related_columns,
            hybrid=self.bags.hybrid_properties
        )

    # Supported operation. Operation name, function that checks params,
    # function that returns condition or another function for call with on cls and conditions.
    # Special operation is '*', which match all operations, used for relations.

    # Operators for scalar (e.g. non-array) columns
    _operators_scalar = {
        # operator => lambda column, value, original_value
        # `original_value` is to be used in conditions, because `val` can be an SQL-expression!
        '$eq':  lambda col, val, oval: col == val,
        '$ne':  lambda col, val, oval: col != val,
        '$lt':  lambda col, val, oval: col < val,
        '$lte': lambda col, val, oval: col <= val,
        '$gt':  lambda col, val, oval: col > val,
        '$gte': lambda col, val, oval: col >= val,
        '$in':  lambda col, val, oval: col.in_(val),  # field IN(values)
        '$nin': lambda col, val, oval: col.notin_(val),  # field NOT IN(values)
        '$exists': lambda col, val, oval: col != None if oval else col == None,
    }

    # Operators for array columns
    _operators_array = {
        # array value: Array equality
        # scalar value: ANY(array) = value
        '$eq':  lambda col, val, oval: col == val if _is_array(oval) else col.any(val),
        # array value: Array inequality
        # scalar value: ALL(array) != value
        '$ne':  lambda col, val, oval: col != val if _is_array(oval) else col.all(val, operators.ne),
        # field && ARRAY[values]
        '$in':  lambda col, val, oval: col.overlap(val),
        # NOT( field && ARRAY[values] )
        '$nin': lambda col, val, oval: ~ col.overlap(val),
        # is not NULL
        '$exists': lambda col, val, oval: col != None if oval else col == None,
        # contains
        '$all': lambda col, val, oval: col.contains(val),
        # value == 0: ARRAY_LENGTH(field, 1) IS NULL
        # value != 0: ARRAY_LENGTH(field, 1) == value
        '$size': lambda col, val, oval: func.array_length(col, 1) == (None if oval == 0 else val),
    }

    # List of operators that always require array argument
    _operators_require_array_value = frozenset(('$all', '$in', '$nin'))

    # List of boolean operators, handled by a separate method
    _boolean_operators = frozenset(('$and', '$or', '$nor', '$not'))

    # These classes implement compilation
    # You can override them, if necessary
    _COLUMN_EXPRESSION_CLS = FilterColumnExpression
    _RELATED_COLUMN_EXPRESSION_CLS = FilterRelatedColumnExpression
    _BOOLEAN_EXPRESSION_CLS = FilterBooleanExpression

    @classmethod
    def add_scalar_operator(cls, name, callable):
        """ Add an operator that operates on scalar columns

            NOTE: This will add an operator that is effective application-wide, which is not good.
                The correct way to do it would be to subclass MongoFilter, or pass
                `scalar_operators` value at __init__() time!

            :param name: Operator name. E.g. '$search'
            :param callable: Function that implements the operator.
                Accepts three arguments: column, processed_value, original_value
        """
        cls._operators_scalar[name] = callable

    @classmethod
    def add_array_operator(cls, name, callable):
        """ Add an operator that operates on array columns """
        cls._operators_array[name] = callable

    def input(self, criteria):
        super(MongoFilter, self).input(criteria)
        self.expressions = self._parse_criteria(criteria)
        return self

    def _parse_criteria(self, criteria):
        """ Parse MongoSQL criteria and return a list of parsed objects.

        This may seem like too much, but this approach
        1) splits parsing and compilation into two logical phases, and
        2) enables you (yes, you) to subclass and change behavior, or hook into the process

        :type criteria: dict | None
        :rtype: list[FilterExpressionBase]
        """
        # None
        if not criteria:
            criteria = {}

        # Validation base
        if not isinstance(criteria, dict):
            raise InvalidQueryError('Filter criteria must be one of: null, object')

        # Transform the boolean expression into a list of conditions
        # In the end, those will be ANDed together
        expressions = []

        # Assuming a dict of mixed { column: value }s and  { column: { $op: value } }s
        for key, criteria in criteria.items():
            # Boolean expressions? ($op: value}
            if key in self._boolean_operators:
                boolean_expression = self._parse_boolean_operator(key, criteria)
                expressions.append(boolean_expression)
                continue  # nothing else to do here

            # Alright, now we're handling a column, not a boolean expression
            # It can, however, be a column on a related model, referenced using the dot-notation:
            # e.g. { parent.id: 10 }. So here we use a combined bag
            column_name = key
            try:
                bag_name, bag, column = self.supported_bags[column_name]
            except KeyError:
                raise InvalidColumnError(self.bags.model_name, column_name, self.query_object_section_name)

            # Fake equality
            # Normally, you're supposed to use '$eq' operator for equality, which has `dict` as
            # an operand. However, because shorthand syntax is supported ({name: "Kevin"}),
            # this is transformed into {name: {$eq: Kevin}} so that we don't have to implement
            # special cases. Lazy, huh?
            if not isinstance(criteria, dict):
                criteria = {'$eq': criteria}  # fake the missing equality operator for simplicity

            # At this point, we have a column, and a dict of multiple criteria.
            # It looks like this:
            # { age: { $gt: 18, $lt: 25 } }
            # Now we got to go through this criteria object, and apply every operator to the column.
            for operator, value in criteria.items():
                # Operator lookup
                try:
                    operator_lambda = self.lookup_operator_lambda(bag.is_column_array(column_name), operator)
                except KeyError:
                    raise InvalidQueryError('Unsupported operator "{}" found in filter for `{}`'
                                            .format(operator, column_name))

                # Validate operator argument
                if operator in self._operators_require_array_value and not _is_array(value):
                    raise InvalidQueryError('Filter: {op} argument must be an array'
                                            .format(op=operator))

                # Handle the result differently depending on the type of column
                # We have to handle relations separately: see compile_statement()
                if bag_name in ('col', 'hybrid'):
                    expressions.append(self._COLUMN_EXPRESSION_CLS(
                        bag, column_name, column,
                        operator, operator_lambda,
                        value
                    ))
                elif bag_name == 'rcol':
                    relation = bag.get_relationship(column_name)
                    relation_name = bag.get_relationship_name(column_name)
                    expressions.append(self._RELATED_COLUMN_EXPRESSION_CLS(
                        bag, relation_name, relation,
                        column_name, column,
                        operator, operator_lambda,
                        value
                    ))
                else:
                    raise NotImplementedError('How did we end up here? Unsupported column type!')

        # Done
        return expressions

    def _parse_boolean_operator(self, op, criteria):
        """ Used in _parse_criteria() to handle boolean operators from self._boolean_operators

            Example:
                Input: { $and: [ {}, ... ] }
                -> _parse_boolean_operator('$and', [ {}, ... ])
        """
        if op == '$not':
            # This operator accepts a dict (not a list), which is a query object itself.
            # Validate
            if not isinstance(criteria, dict):
                raise InvalidQueryError('{}: $not argument must be an object'
                                        .format(self.query_object_section_name))

            # Recurse
            criterion = self._parse_criteria(criteria)

            # Done
            return self._BOOLEAN_EXPRESSION_CLS(op, criterion)
        else:
            # All other operators accept a list: $and, $or, $nor
            # Validate it's a list
            if not isinstance(criteria, (list, tuple)):
                raise InvalidQueryError('{}: {} argument must be a list'
                                        .format(self.query_object_section_name, op))

            # Because the argument of a boolean expression is always a list of other query objects,
            # we have to recurse here and parse it.
            # Example: { $or: [ {..}, {..}, {..} ]}
            #   will have to call _parse_criteria() for every object within: recursion
            # Note that we never validate `s`, because _parse_criteria() will do it for us.
            criteria = [self._parse_criteria(s) for s in criteria]  # type criteria: FilterExpressionBase

            # Done
            if len(criteria) == 0:
                return None  # Empty criteria: { $or: [] } or something like this that does not make sense
            else:
                return self._BOOLEAN_EXPRESSION_CLS(op, criteria)

    def lookup_operator_lambda(self, column_is_array, operator):
        """ Lookup an operator in `self`, or extra operators

        :param column_is_array: Is the column an ARRAY column?
            Lookup will be limited to array operators
        :param operator: Operator string
        :return: lambda
        :raises: KeyError
        """
        if not column_is_array:
            return self._operators_scalar.get(operator) or self._extra_scalar_ops[operator]
        else:
            return self._operators_array.get(operator) or self._extra_array_ops[operator]

    def compile_statement(self):
        """ Create an SQL statement

        :rtype: sqlalchemy.sql.elements.BooleanClauseList
        """
        # The list of conditions that will be created by parsing the Query object.
        # In the end, those will be ANDed together
        conditions = []

        # Alright, first we have to handle conditions applied to relationships
        # We have to handle them separately because we want to group filters on the same
        # relationship. If we don't, it may generate duplicate subqueries, for every condition.
        # This would've been not good.
        # So what we do here is we split `expressions` into two groups:
        # 1. Column expressions
        # 2. Relationship expressions, grouped by relation name
        column_expressions = []
        relationship_expressions = {}
        for e in self.expressions:
            if isinstance(e, FilterRelatedColumnExpression):
                relationship_expressions.setdefault(e.relation_name, [])
                relationship_expressions[e.relation_name].append(e)
            else:
                column_expressions.append(e)

        # Compile column expressions. Easy
        conditions.extend(e.compile_expression() for e in column_expressions)

        # Compile related column expressions, grouped by their relation name
        for rel_name, expressions in relationship_expressions.items():
            # Compile
            rel_conditions = [e.compile_expression() for e in expressions]

            # Now, build one query for the whole relationship
            relationship = self.bags.relations[rel_name]
            if self.bags.relations.is_relationship_array(rel_name):
                conditions.append(relationship.any(and_(*rel_conditions)))
            else:
                conditions.append(relationship.has(and_(*rel_conditions)))

        # Convert the list of conditions to one final expression
        return self._BOOLEAN_EXPRESSION_CLS.sql_anded_together(conditions)
