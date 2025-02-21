#pragma once

#include <Common/Arena.h>
#include <Common/PODArray.h>
#include <Columns/IColumn.h>
#include <Columns/ColumnsCommon.h>
#include <Core/Field.h>


namespace DB
{

namespace ErrorCodes
{
    extern const int SIZES_OF_COLUMNS_DOESNT_MATCH;
    extern const int NOT_IMPLEMENTED;
}


/** Base class for columns-constants that contain a value that is not in the `Field`.
  * Not a full-fledged column and is used in a special way.
  */
class IColumnDummy : public IColumn
{
public:
    IColumnDummy() : s(0) {}
    explicit IColumnDummy(size_t s_) : s(s_) {}

    virtual MutableColumnPtr cloneDummy(size_t s_) const = 0;

    MutableColumnPtr cloneResized(size_t s_) const override { return cloneDummy(s_); }
    size_t size() const override { return s; }
    void insertDefault() override { ++s; }
    void popBack(size_t n) override { s -= n; }
    size_t byteSize() const override { return 0; }
    size_t byteSizeAt(size_t) const override { return 0; }
    size_t allocatedBytes() const override { return 0; }
    int compareAt(size_t, size_t, const IColumn &, int) const override { return 0; }
    void compareColumn(const IColumn &, size_t, PaddedPODArray<UInt64> *, PaddedPODArray<Int8> &, int, int) const override
    {
    }

    bool hasEqualValues() const override { return true; }

    Field operator[](size_t) const override { throw Exception(ErrorCodes::NOT_IMPLEMENTED, "Cannot get value from {}", getName()); }
    void get(size_t, Field &) const override { throw Exception(ErrorCodes::NOT_IMPLEMENTED, "Cannot get value from {}", getName()); }
    void insert(const Field &) override { throw Exception(ErrorCodes::NOT_IMPLEMENTED, "Cannot insert element into {}", getName()); }
    bool isDefaultAt(size_t) const override { throw Exception(ErrorCodes::NOT_IMPLEMENTED, "isDefaultAt is not implemented for {}", getName()); }

    StringRef getDataAt(size_t) const override
    {
        return {};
    }

    void insertData(const char *, size_t) override
    {
        ++s;
    }

    StringRef serializeValueIntoArena(size_t /*n*/, Arena & arena, char const *& begin) const override
    {
        /// Has to put one useless byte into Arena, because serialization into zero number of bytes is ambiguous.
        char * res = arena.allocContinue(1, begin);
        *res = 0;
        return { res, 1 };
    }

    const char * deserializeAndInsertFromArena(const char * pos) override
    {
        ++s;
        return pos + 1;
    }

    const char * skipSerializedInArena(const char * pos) const override
    {
        return pos;
    }

    void updateHashWithValue(size_t /*n*/, SipHash & /*hash*/) const override
    {
    }

    void updateWeakHash32(WeakHash32 & /*hash*/) const override
    {
    }

    void updateHashFast(SipHash & /*hash*/) const override
    {
    }

    void insertFrom(const IColumn &, size_t) override
    {
        ++s;
    }

    void insertRangeFrom(const IColumn & /*src*/, size_t /*start*/, size_t length) override
    {
        s += length;
    }

    ColumnPtr filter(const Filter & filt, ssize_t /*result_size_hint*/) const override
    {
        size_t bytes = countBytesInFilter(filt);
        return cloneDummy(bytes);
    }

    void expand(const IColumn::Filter & mask, bool inverted) override
    {
        size_t bytes = countBytesInFilter(mask);
        if (inverted)
            bytes = mask.size() - bytes;
        s = bytes;
    }

    ColumnPtr permute(const Permutation & perm, size_t limit) const override
    {
        if (s != perm.size())
            throw Exception(ErrorCodes::SIZES_OF_COLUMNS_DOESNT_MATCH, "Size of permutation doesn't match size of column.");

        return cloneDummy(limit ? std::min(s, limit) : s);
    }

    ColumnPtr index(const IColumn & indexes, size_t limit) const override
    {
        if (indexes.size() < limit)
            throw Exception(ErrorCodes::SIZES_OF_COLUMNS_DOESNT_MATCH, "Size of indexes is less than required.");

        return cloneDummy(limit ? limit : s);
    }

    void getPermutation(IColumn::PermutationSortDirection /*direction*/, IColumn::PermutationSortStability /*stability*/,
                    size_t /*limit*/, int /*nan_direction_hint*/, Permutation & res) const override
    {
        res.resize(s);
        for (size_t i = 0; i < s; ++i)
            res[i] = i;
    }

    void updatePermutation(IColumn::PermutationSortDirection /*direction*/, IColumn::PermutationSortStability /*stability*/,
                    size_t, int, Permutation &, EqualRanges&) const override {}

    ColumnPtr replicate(const Offsets & offsets) const override
    {
        if (s != offsets.size())
            throw Exception(ErrorCodes::SIZES_OF_COLUMNS_DOESNT_MATCH, "Size of offsets doesn't match size of column.");

        return cloneDummy(offsets.back());
    }

    MutableColumns scatter(ColumnIndex num_columns, const Selector & selector) const override
    {
        if (s != selector.size())
            throw Exception(ErrorCodes::SIZES_OF_COLUMNS_DOESNT_MATCH, "Size of selector doesn't match size of column.");

        std::vector<size_t> counts(num_columns);
        for (auto idx : selector)
            ++counts[idx];

        MutableColumns res(num_columns);
        for (size_t i = 0; i < num_columns; ++i)
            res[i] = cloneResized(counts[i]);

        return res;
    }

    double getRatioOfDefaultRows(double) const override
    {
        throw Exception(ErrorCodes::NOT_IMPLEMENTED, "Method getRatioOfDefaultRows is not supported for {}", getName());
    }

    void getIndicesOfNonDefaultRows(Offsets &, size_t, size_t) const override
    {
        throw Exception(ErrorCodes::NOT_IMPLEMENTED, "Method getIndicesOfNonDefaultRows is not supported for {}", getName());
    }

    void gather(ColumnGathererStream &) override
    {
        throw Exception(ErrorCodes::NOT_IMPLEMENTED, "Method gather is not supported for {}", getName());
    }

    void getExtremes(Field &, Field &) const override
    {
    }

    void addSize(size_t delta)
    {
        s += delta;
    }

    bool isDummy() const override
    {
        return true;
    }

protected:
    size_t s;
};

}
