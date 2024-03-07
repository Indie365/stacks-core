use proptest::prelude::*;

use super::*;
use crate::vm::representations::{Span, TraitDefinition};
use crate::vm::{ClarityName, ContractName, SymbolicExpression, SymbolicExpressionType};

/// Returns a [`Strategy`] for randomly generating a [`ClarityName`].
pub fn clarity_name() -> impl Strategy<Value = ClarityName> {
    "[a-z]{40}".prop_map(|s| s.try_into().unwrap())
}

/// Returns a [`Strategy`] for randomly generating a [`ContractName`].
pub fn contract_name() -> impl Strategy<Value = ContractName> {
    "[a-zA-Z]{1,40}".prop_map(|s| s.try_into().unwrap())
}

/// Returns a [`Strategy`] for randomly generating a [`TraitDefinition`].
pub fn trait_definition() -> impl Strategy<Value = TraitDefinition> {
    prop_oneof![
        trait_identifier().prop_map(TraitDefinition::Defined),
        trait_identifier().prop_map(TraitDefinition::Imported)
    ]
}

/// Returns a [`Strategy`] for randomly generating a [`SymbolicExpression`].
pub fn symbolic_expression() -> impl Strategy<Value = SymbolicExpression> {
    let leaf = prop_oneof![
        clarity_name().prop_map(|name| SymbolicExpression::atom(name)),
        PropValue::any().prop_map(|val| SymbolicExpression::atom_value(val.into())),
        PropValue::any().prop_map(|val| SymbolicExpression::literal_value(val.into())),
        trait_identifier().prop_map(|name| SymbolicExpression::field(name)),
        (clarity_name(), trait_definition())
            .prop_map(|(n, t)| SymbolicExpression::trait_reference(n, t)),
    ];

    leaf.prop_recursive(3, 64, 5, |inner| {
        prop::collection::vec(inner, 1..3)
            .prop_map(|list| SymbolicExpression::list(list.into_boxed_slice()))
    })
}
