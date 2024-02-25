// Copyright (C) 2013-2020 Blockstack PBC, a public benefit corporation
// Copyright (C) 2020 Stacks Open Internet Foundation
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <http://www.gnu.org/licenses/>.

use std::error::Error as ErrorTrait;
use std::string::FromUtf8Error;
use std::{error, fmt};

use rusqlite::Error as SqliteError;
use serde_json::Error as SerdeJSONErr;
use stacks_common::types::chainstate::BlockHeaderHash;

use super::analysis::CheckError;
use super::ast::errors::ParseErrors;
pub use crate::vm::analysis::errors::{
    check_argument_count, check_arguments_at_least, check_arguments_at_most, CheckErrors,
};
use crate::vm::ast::errors::ParseError;
use crate::vm::contexts::StackTrace;
use crate::vm::costs::CostErrors;
use crate::vm::types::{TypeSignature, Value};

#[derive(Debug)]
pub struct IncomparableError<T> {
    pub err: T,
}

#[derive(Debug)]
pub enum Error {
    /// UncheckedErrors are errors that *should* be caught by the
    ///   TypeChecker and other check passes. Test executions may
    ///   trigger these errors.
    Unchecked(CheckErrors),
    Interpreter(InterpreterError),
    Runtime(RuntimeErrorType, Option<StackTrace>),
    ShortReturn(ShortReturnType),
}

/// InterpreterErrors are errors that *should never* occur.
/// Test executions may trigger these errors.
#[derive(Debug, PartialEq)]
pub enum InterpreterError {
    BadSender(Value),
    BadSymbolicRepresentation(String),
    InterpreterError(String),
    UninitializedPersistedVariable,
    FailedToConstructAssetTable,
    FailedToConstructEventBatch,
    SqliteError(IncomparableError<SqliteError>),
    BadFileName,
    FailedToCreateDataDirectory,
    MarfFailure(String),
    FailureConstructingTupleWithType,
    FailureConstructingListWithType,
    InsufficientBalance,
    CostContractLoadFailure,
    DBError(String),
    Expect(String),
}

impl fmt::Display for InterpreterError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            InterpreterError::BadSender(x) => write!(f, "Bad sender: {}", x),
            InterpreterError::BadSymbolicRepresentation(x) => {
                write!(f, "Bad symbolic representation: {}", x)
            },
            InterpreterError::InterpreterError(x) => write!(f, "Interpreter error: {}", x),
            InterpreterError::UninitializedPersistedVariable => {
                write!(f, "Uninitialized persisted variable")
            },
            InterpreterError::FailedToConstructAssetTable => {
                write!(f, "Failed to construct asset table")
            },
            InterpreterError::FailedToConstructEventBatch => {
                write!(f, "Failed to construct event batch")
            },
            InterpreterError::BadFileName => write!(f, "Bad file name"),
            InterpreterError::FailedToCreateDataDirectory => {
                write!(f, "Failed to create data directory")
            },
            InterpreterError::MarfFailure(x) => write!(f, "Marf failure: {}", x),
            InterpreterError::FailureConstructingTupleWithType => {
                write!(f, "Failure constructing tuple with type")
            },
            InterpreterError::FailureConstructingListWithType => {
                write!(f, "Failure constructing list with type")
            },
            InterpreterError::InsufficientBalance => write!(f, "Insufficient balance"),
            InterpreterError::CostContractLoadFailure => write!(f, "Cost contract load failure"),
            InterpreterError::DBError(x) => write!(f, "DB error: {}", x),
            InterpreterError::Expect(x) => write!(f, "Expect: {}", x),
            InterpreterError::SqliteError(e) => write!(f, "SqliteError: {}", e.err),
        }
    }
}

/// RuntimeErrors are errors that smart contracts are expected
///   to be able to trigger during execution (e.g., arithmetic errors)
#[derive(Debug, PartialEq)]
pub enum RuntimeErrorType {
    Arithmetic(String),
    ArithmeticOverflow,
    ArithmeticUnderflow,
    SupplyOverflow(u128, u128),
    SupplyUnderflow(u128, u128),
    DivisionByZero,
    // error in parsing types
    ParseError(String),
    // error in parsing the AST
    ASTError(ParseError),
    MaxStackDepthReached,
    MaxContextDepthReached,
    ListDimensionTooHigh,
    BadTypeConstruction,
    ValueTooLarge,
    BadBlockHeight(String),
    TransferNonPositiveAmount,
    NoSuchToken,
    NotImplemented,
    NoCallerInContext,
    NoSenderInContext,
    NonPositiveTokenSupply,
    JSONParseError(IncomparableError<SerdeJSONErr>),
    AttemptToFetchInTransientContext,
    BadNameValue(&'static str, String),
    UnknownBlockHeaderHash(BlockHeaderHash),
    BadBlockHash(Vec<u8>),
    UnwrapFailure,
    DefunctPoxContract,
    PoxAlreadyLocked,
    MetadataAlreadySet,
}

#[derive(Debug, PartialEq)]
pub enum ShortReturnType {
    ExpectedValue(Value),
    AssertionFailed(Value),
}

pub type InterpreterResult<R> = Result<R, Error>;

impl From<Error> for CheckError {
    fn from(err: Error) -> Self {
        match err {
            Error::Unchecked(e) => e.into(),
            Error::Interpreter(e) => e.into(),
            Error::ShortReturn(e) => panic!("We should not be converting from ShortReturn errors to CheckErrors ({e:?})"),
            Error::Runtime(e, f) => panic!("We should not be converting from Runtime errors to CheckErrors ({e:?}) ({f:?})"),
        }
    }

}

impl<T> PartialEq<IncomparableError<T>> for IncomparableError<T> {
    fn eq(&self, _other: &IncomparableError<T>) -> bool {
        return false;
    }
}

impl From<lz4_flex::block::CompressError> for Error {
    fn from(err: lz4_flex::block::CompressError) -> Self {
        Error::Interpreter(InterpreterError::Expect(format!(
            "Compression failed: {}",
            err
        )))
    }
}

impl From<lz4_flex::block::DecompressError> for Error {
    fn from(err: lz4_flex::block::DecompressError) -> Self {
        Error::Interpreter(InterpreterError::Expect(format!(
            "Decompression failed: {}",
            err
        )))
    }
}

impl From<FromUtf8Error> for Error {
    fn from(err: FromUtf8Error) -> Self {
        Error::Interpreter(InterpreterError::Expect(format!(
            "Failed to convert bytes to string: {}",
            err
        )))
    }
}

impl From<rusqlite::Error> for Error {
    fn from(err: rusqlite::Error) -> Self {
        Error::Interpreter(InterpreterError::SqliteError(IncomparableError { err }))
    }
}

impl From<lzzzz::Error> for Error {
    fn from(err: lzzzz::Error) -> Self {
        Error::Interpreter(InterpreterError::Expect(format!(
            "Compression failed: {}",
            err
        )))
    }
}

impl PartialEq<Error> for Error {
    fn eq(&self, other: &Error) -> bool {
        match (self, other) {
            (Error::Runtime(x, _), Error::Runtime(y, _)) => x == y,
            (Error::Unchecked(x), Error::Unchecked(y)) => x == y,
            (Error::ShortReturn(x), Error::ShortReturn(y)) => x == y,
            (Error::Interpreter(x), Error::Interpreter(y)) => x == y,
            _ => false,
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        match self {
            Error::Runtime(ref err, ref stack) => {
                match err {
                    _ => write!(f, "{}", err),
                }?;

                if let Some(ref stack_trace) = stack {
                    write!(f, "\n Stack Trace: \n")?;
                    for item in stack_trace.iter() {
                        write!(f, "{}\n", item)?;
                    }
                }
                Ok(())
            }
            _ => write!(f, "{:?}", self),
        }
    }
}

impl fmt::Display for RuntimeErrorType {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:?}", self)
    }
}

impl error::Error for Error {
    fn source(&self) -> Option<&(dyn error::Error + 'static)> {
        None
    }
}

impl error::Error for RuntimeErrorType {
    fn source(&self) -> Option<&(dyn error::Error + 'static)> {
        None
    }
}

impl From<ParseError> for Error {
    fn from(err: ParseError) -> Self {
        match &err.err {
            ParseErrors::InterpreterFailure => Error::from(InterpreterError::Expect(
                "Unexpected interpreter failure during parsing".into(),
            )),
            _ => Error::from(RuntimeErrorType::ASTError(err)),
        }
    }
}

impl From<CostErrors> for Error {
    fn from(err: CostErrors) -> Self {
        match err {
            CostErrors::InterpreterFailure => Error::from(InterpreterError::Expect(
                "Interpreter failure during cost calculation".into(),
            )),
            CostErrors::Expect(s) => Error::from(InterpreterError::Expect(format!(
                "Interpreter failure during cost calculation: {s}"
            ))),
            other_err => Error::from(CheckErrors::from(other_err)),
        }
    }
}

impl From<RuntimeErrorType> for Error {
    fn from(err: RuntimeErrorType) -> Self {
        Error::Runtime(err, None)
    }
}

impl From<CheckErrors> for Error {
    fn from(err: CheckErrors) -> Self {
        Error::Unchecked(err)
    }
}

impl From<ShortReturnType> for Error {
    fn from(err: ShortReturnType) -> Self {
        Error::ShortReturn(err)
    }
}

impl From<InterpreterError> for Error {
    fn from(err: InterpreterError) -> Self {
        Error::Interpreter(err)
    }
}

#[cfg(test)]
impl From<Error> for () {
    fn from(err: Error) -> Self {}
}

impl Into<Value> for ShortReturnType {
    fn into(self) -> Value {
        match self {
            ShortReturnType::ExpectedValue(v) => v,
            ShortReturnType::AssertionFailed(v) => v,
        }
    }
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::vm::execute;

    #[test]
    #[cfg(feature = "developer-mode")]
    fn error_formats() {
        let t = "(/ 10 0)";
        let expected = "DivisionByZero
 Stack Trace: 
_native_:native_div
";

        assert_eq!(format!("{}", execute(t).unwrap_err()), expected);
    }

    #[test]
    fn equality() {
        assert_eq!(
            Error::ShortReturn(ShortReturnType::ExpectedValue(Value::Bool(true))),
            Error::ShortReturn(ShortReturnType::ExpectedValue(Value::Bool(true)))
        );
        assert_eq!(
            Error::Interpreter(InterpreterError::InterpreterError("".to_string())),
            Error::Interpreter(InterpreterError::InterpreterError("".to_string()))
        );
        assert!(
            Error::ShortReturn(ShortReturnType::ExpectedValue(Value::Bool(true)))
                != Error::Interpreter(InterpreterError::InterpreterError("".to_string()))
        );
    }
}
