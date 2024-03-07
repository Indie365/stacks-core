use proptest::prop_oneof;
use proptest::strategy::{Just, Strategy, ValueTree};
use proptest::test_runner::{Config, RngAlgorithm, TestRng, TestRunner};
use rand::Rng;
use stacks_common::types::StacksHashMap as HashMap;

pub mod callables;
pub mod contracts;
pub mod representations;
pub mod types;
pub mod values;

pub use callables::*;
pub use contracts::*;
pub use representations::*;
pub use types::*;
pub use values::*;

use crate::vm::ClarityVersion;

/// Returns a [`Strategy`] for randomly generating a [`ClarityVersion`] instance.
pub fn clarity_version() -> impl Strategy<Value = ClarityVersion> {
    prop_oneof![
        Just(crate::vm::ClarityVersion::Clarity1),
        Just(crate::vm::ClarityVersion::Clarity2),
    ]
}
