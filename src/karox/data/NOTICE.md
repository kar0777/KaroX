# Third-party data

`models_dev.json.gz` is a compact, dated snapshot of the provider and model
catalog published by [models.dev](https://models.dev) (the catalog OpenCode
uses), reduced by `scripts/update_provider_catalog.py` to the fields KaroX
needs: provider name, endpoint, key variable names, and per model its context
window, output limit, capabilities and published list price.

models.dev is distributed under the MIT License:

    MIT License

    Copyright (c) 2025 models.dev

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

Prices are the list prices recorded on the snapshot date. KaroX uses them for
estimates and budgets only and never overrides a price entered by the user.
