# A2UI v0.9 screens — the Basic Catalog Kyllene draws

Catalog: `https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json`. 18 components.
Messages, in order: `createSurface`, `updateComponents` (a flat list; one component's id is `root`),
`updateDataModel`, `deleteSurface`. A property bound to the data model is `{"path": "/name"}`.

## Text

`Text {text, variant: h1, h2, h3, h4, h5, caption, body}`

- **text** — The text content to display. While simple Markdown formatting is supported (i.e. without HTML, images, or links), utilizing dedicated UI components is generally preferred for a richer and more structured presentation.
- **variant** (h1, h2, h3, h4, h5, caption, body) — A hint for the base text style.

## Image

`Image {url, description, fit: contain, cover, fill, none, scaleDown, variant: icon, avatar, smallFeature, mediumFeature, largeFeature, header}`

- **url** — The URL of the image to display.
- **description** — Accessibility text for the image.
- **fit** (contain, cover, fill, none, scaleDown) — Specifies how the image should be resized to fit its container. This corresponds to the CSS 'object-fit' property.
- **variant** (icon, avatar, smallFeature, mediumFeature, largeFeature, header) — A hint for the image size and style.

## Icon

`Icon {name: accountCircle, add, arrowBack, arrowForward, attachFile, calendarToday, call, camera, check, close, delete, download, edit, event, error, fastForward, favorite, favoriteOff, folder, help, home, info, locationOn, lock, lockOpen, mail, menu, moreVert, moreHoriz, notificationsOff, notifications, pause, payment, person, phone, photo, play, print, refresh, rewind, search, send, settings, share, shoppingCart, skipNext, skipPrevious, star, starHalf, starOff, stop, upload, visibility, visibilityOff, volumeDown, volumeMute, volumeOff, volumeUp, warning}`

- **name** (accountCircle, add, arrowBack, arrowForward, attachFile, calendarToday, call, camera, check, close, delete, download, edit, event, error, fastForward, favorite, favoriteOff, folder, help, home, info, locationOn, lock, lockOpen, mail, menu, moreVert, moreHoriz, notificationsOff, notifications, pause, payment, person, phone, photo, play, print, refresh, rewind, search, send, settings, share, shoppingCart, skipNext, skipPrevious, star, starHalf, starOff, stop, upload, visibility, visibilityOff, volumeDown, volumeMute, volumeOff, volumeUp, warning) — The name of the icon to display.

## Video

`Video {url}`

- **url** — The URL of the video to display.

## AudioPlayer

`AudioPlayer {url, description}`

- **url** — The URL of the audio to be played.
- **description** — A description of the audio, such as a title or summary.

## Row

`Row {children, justify: center, end, spaceAround, spaceBetween, spaceEvenly, start, stretch, align: start, center, end, stretch}`

- **children** — Defines the children. Use an array of strings for a fixed set of children, or a template object to generate children from a data list. Children cannot be defined inline, they must be referred to by ID.
- **justify** (center, end, spaceAround, spaceBetween, spaceEvenly, start, stretch) — Defines the arrangement of children along the main axis (horizontally). Use 'spaceBetween' to push items to the edges, or 'start'/'end'/'center' to pack them together.
- **align** (start, center, end, stretch) — Defines the alignment of children along the cross axis (vertically). This is similar to the CSS 'align-items' property, but uses camelCase values (e.g., 'start').

## Column

`Column {children, justify: start, center, end, spaceBetween, spaceAround, spaceEvenly, stretch, align: center, end, start, stretch}`

- **children** — Defines the children. Use an array of strings for a fixed set of children, or a template object to generate children from a data list. Children cannot be defined inline, they must be referred to by ID.
- **justify** (start, center, end, spaceBetween, spaceAround, spaceEvenly, stretch) — Defines the arrangement of children along the main axis (vertically). Use 'spaceBetween' to push items to the edges (e.g. header at top, footer at bottom), or 'start'/'end'/'center' to pack them together.
- **align** (center, end, start, stretch) — Defines the alignment of children along the cross axis (horizontally). This is similar to the CSS 'align-items' property.

## List

`List {children, direction: vertical, horizontal, align: start, center, end, stretch, listStyle: ordered, unordered, none}`

- **children** — Defines the children. Use an array of strings for a fixed set of children, or a template object to generate children from a data list.
- **direction** (vertical, horizontal) — The direction in which the list items are laid out.
- **align** (start, center, end, stretch) — Defines the alignment of children along the cross axis.
- **listStyle** (ordered, unordered, none) — The style of the list (ordered, unordered, or none).

## Card

`Card {child}`

- **child** — The ID of the single child component to be rendered inside the card. To display multiple elements, you MUST wrap them in a layout component (like Column or Row) and pass that container's ID here. Do NOT pass multiple IDs or a non-existent ID.

## Tabs

`Tabs {tabs: [{title, child}]}`

- **tabs** ([{title, child}]) — An array of objects, where each object defines a tab with a title and a child component.

## Modal

`Modal {trigger, content}`

- **trigger** — The ID of the component that opens the modal when interacted with (e.g., a button).
- **content** — The ID of the component to be displayed inside the modal.

## Divider

`Divider {axis: horizontal, vertical}`

- **axis** (horizontal, vertical) — The orientation of the divider.

## Button

`Button {child, variant: default, primary, borderless, action}`

- **child** — The ID of the child component. Use a 'Text' component for a labeled button. Only use an 'Icon' if the requirements explicitly ask for an icon-only button.
- **variant** (default, primary, borderless) — A hint for the button style. If omitted, a default button style is used. 'primary' indicates this is the main call-to-action button. 'borderless' means the button has no visual border or background, making its child content appear like a clickable link.
- **action**

## TextField

`TextField {label, value, variant: longText, number, shortText, obscured, validationRegexp: string}`

- **label** — The text label for the input field.
- **value** — The value of the text field.
- **variant** (longText, number, shortText, obscured) — The type of input field to display.
- **validationRegexp** (string) — A regular expression used for client-side validation of the input.

## CheckBox

`CheckBox {label, value}`

- **label** — The text to display next to the checkbox.
- **value** — The current state of the checkbox (true for checked, false for unchecked).

## ChoicePicker

`ChoicePicker {label, variant: multipleSelection, mutuallyExclusive, options: [{label, value}], value, displayStyle: checkbox, chips, filterable: boolean}`

- **label** — The label for the group of options.
- **variant** (multipleSelection, mutuallyExclusive) — A hint for how the choice picker should be displayed and behave.
- **options** ([{label, value}]) — The list of available options to choose from.
- **value** — The list of currently selected values. This should be bound to a string array in the data model.
- **displayStyle** (checkbox, chips) — The display style of the component.
- **filterable** (boolean) — If true, displays a search input to filter the options.

## Slider

`Slider {label, min: number, max: number, value}`

- **label** — The label for the slider.
- **min** (number) — The minimum value of the slider.
- **max** (number) — The maximum value of the slider.
- **value** — The current value of the slider.

## DateTimeInput

`DateTimeInput {value, enableDate: boolean, enableTime: boolean, min, max, label}`

- **value** — The selected date and/or time value in ISO 8601 format. If not yet set, initialize with an empty string.
- **enableDate** (boolean) — If true, allows the user to select a date.
- **enableTime** (boolean) — If true, allows the user to select a time.
- **min** — The minimum allowed date/time in ISO 8601 format.
- **max** — The maximum allowed date/time in ISO 8601 format.
- **label** — The text label for the input field.
